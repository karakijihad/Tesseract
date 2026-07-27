"""Operator ask-gate — approval closures, cost-overage gate, and parked-ask machinery (trio W4)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from aiohttp import web
from pydantic import BaseModel

from tesseract.brain.tools import AskFn
from tesseract.kernel.tools.base import CliSink, Tool, ToolContext
from tesseract.mirror.server.envelope import make_envelope
from tesseract.mirror.server.event_log import EventLog
from tesseract.mirror.server.session_model import ParkedAsk
from tesseract.permissions import approval_log

log = logging.getLogger(__name__)

ASK_TIMEOUT_SECONDS = 30.0
# Grace window after the primary timeout fires — a click in flight at the
# moment the timer expires would otherwise hit the just-popped future and
# silently drop ("operator declined" surfaces despite the operator saying
# yes a fraction of a second before t=30.0). 1.5s covers a realistic WS
# round-trip + render latency without meaningfully prolonging the wait.
ASK_GRACE_SECONDS = 1.5


def _make_status_emit(ws: web.WebSocketResponse, session_id: str, event_log: EventLog):
    """Build a per-session async callback for ToolContext.status_emit.

    Tools fire it with a one-line "what's happening" string ("delegating to
    vision_agent (<resolved-model>)…", "calling image_generator
    (<resolved-model>)…"). The model name is interpolated at call time from
    the role's resolved primary — the docstring stays role-centric so it
    doesn't rot when providers swap.
    The callback wraps that in a `tool_status` envelope on the same WS the
    chat stream rides — frontend pulse / chat surface can render it to give
    the operator visibility into role + model during the round-trip.
    """
    from tesseract.mirror.server.envelope import make_envelope

    async def status_emit(message: str) -> None:
        envelope = make_envelope(
            "tool_status", "loop", session_id, {"message": message},
        )
        event_log.append(envelope)
        if ws.closed:
            return
        try:
            await ws.send_json(envelope)
        except ConnectionResetError:
            log.debug("ws closed mid-status for %s", session_id)
    return status_emit


class ChatInfraNotReady(RuntimeError):
    """Raised when a chat session is requested before ``_build_chat_infra``
    has populated ``app['adapter_entry']`` (the brain-adapter build + manifest
    prompt assembly take ~20-30s at boot, during which the WS listener is
    already accepting connections). Transient — callers should ask the client
    to reconnect once chat infra is ready rather than surfacing a crash."""


def _make_overage_ask_fn(
    ws: web.WebSocketResponse,
    session_id: str,
    pending: dict[str, asyncio.Future[bool]],
    event_log: EventLog,
):
    """Cost-overage approval callback. Mirrors `_make_ask_fn` but emits
    `cost_overage_ask` envelopes and waits for `cost_overage_response`
    via the `pending` dict. Timeout = same `ASK_TIMEOUT_SECONDS` as
    tool asks; on timeout we treat the silence as DENY so an unattended
    operator never auto-approves overage spend."""
    from tesseract.brain.cost import BudgetExhausted as _BE

    async def overage_ask_fn(exc: _BE) -> bool:
        scope_key = exc.scope_key()
        # Human-readable label mirrors the scope_key but is friendlier
        # in the toast/card. Voice scopes already include kind+provider.
        scope_label = (
            "global daily budget"
            if exc.scope == "global"
            else exc.role
            if exc.scope == "voice"
            else f"role {exc.role}"
        )
        call_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        pending[call_id] = fut

        ask_env = make_envelope(
            "cost_overage_ask",
            "cost",
            session_id,
            {
                "call_id": call_id,
                "scope_key": scope_key,
                "scope_label": scope_label,
                "spent_usd": exc.spent_usd,
                "cap_usd": exc.cap_usd,
            },
        )
        event_log.append(ask_env)
        if not ws.closed:
            try:
                await ws.send_json(ask_env)
            except ConnectionResetError:
                pending.pop(call_id, None)
                return False

        try:
            approved = await asyncio.wait_for(fut, timeout=ASK_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            approved = False
            log.info(
                "overage_ask timeout (denied): scope=%s call_id=%s",
                scope_key, call_id,
            )
        finally:
            pending.pop(call_id, None)
        return approved

    return overage_ask_fn


def _make_cli_sink(
    ws: web.WebSocketResponse,
    session_id: str,
    event_log: EventLog,
) -> CliSink:
    async def cli_sink(event_name: str, call_id: str, payload: dict[str, Any]) -> None:
        env = make_envelope(
            event_name,
            "cli",
            session_id,
            {"call_id": call_id, **payload},
        )
        event_log.append(env)
        if ws.closed:
            return
        try:
            await ws.send_json(env)
        except ConnectionResetError:
            log.debug("ws closed mid-cli-emit for %s", session_id)

    return cli_sink


def _spawn_handle_id_of_current_task() -> str | None:
    """trio W4 — background-spawn origin detection. `SpawnRegistry.register`
    names every spawn task `spawn:<handle_id>` (load-bearing contract,
    documented at the mint site), and `chat.py::_run_pending_calls` carries
    the prefix onto its fan-out tool tasks as `spawn:<id>|tool:…` — an ASK
    running inside either belongs to background work and parks on timeout
    instead of dying."""
    task = asyncio.current_task()
    if task is None:
        return None
    name = task.get_name()
    if name.startswith("spawn:"):
        return name.split("|", 1)[0].removeprefix("spawn:")
    return None


def _make_ask_fn(
    ws: web.WebSocketResponse,
    session_id: str,
    pending_asks: dict[str, asyncio.Future[bool]],
    event_log: EventLog,
    parked_asks: dict[str, "ParkedAsk"] | None = None,
) -> AskFn:
    async def ask_fn(tool: Tool, validated: BaseModel, context: ToolContext) -> bool:
        call_id = context.current_call_id or uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        pending_asks[call_id] = fut

        raw_input = validated.model_dump()
        ask_env = make_envelope(
            "tool_ask",
            "execution",
            session_id,
            {
                "call_id": call_id,
                "name": tool.name,
                "input": raw_input,
                "reason": "",
            },
        )
        event_log.append(ask_env)
        # M4 — fail-soft: a closed/broken originating socket must not raise here
        # before a background-spawn ask reaches its parking path (the exact
        # disconnect-before-park case ask-instead-of-die exists to survive). The
        # envelope is already in the event_log; parking re-emits on reconnect.
        try:
            if not ws.closed:
                await ws.send_json(ask_env)
        except Exception:
            log.debug("tool_ask initial send failed for %s", call_id, exc_info=True)

        timed_out = False
        cancelled = False
        approved = False
        # trio W4 — set when the ask parked: the FINAL ledger row/envelope
        # must reflect the eventual decision, never a stale "timeout".
        park_result: str | None = None

        async def _park_and_wait(spawn_handle_id: str | None) -> tuple[bool, str]:
            """Ask-instead-of-die: hold the SAME future in the approvals pane
            for up to runtime.yaml::ask_park_timeout_s. Returns
            (approved, result_label ∈ allow_once|deny|park_timeout)."""
            from tesseract.brain.spawns import find_handle, mark_input_required
            from tesseract.config.runtime_limits import (
                default_runtime_config_path,
                load_ask_park_timeout_s,
            )

            park_timeout_s = load_ask_park_timeout_s(default_runtime_config_path())
            handle = find_handle(spawn_handle_id) if spawn_handle_id else None
            entry = ParkedAsk(
                call_id=call_id,
                session_id=session_id,
                tool_name=tool.name,
                input_summary=approval_log.summarize_input(raw_input),
                spawn_handle_id=spawn_handle_id,
                parked_at=datetime.now(timezone.utc).isoformat(),
                future=fut,
            )
            if parked_asks is not None:
                # M13 — key by the minted approval_id, not the bare call_id, so
                # a colliding call_id from another session can't overwrite this.
                parked_asks[entry.approval_id] = entry
            if handle is not None:
                mark_input_required(handle, True)
            if spawn_handle_id:
                # Journal the park (best-effort) so a restart-time
                # `sweep_orphans` can say "was parked awaiting operator
                # input" instead of the generic vanished message.
                from tesseract.brain import spawn_journal

                spawn_journal.record_parked(session_id, spawn_handle_id)
            parked_env = make_envelope(
                "tool_ask_parked",
                "execution",
                session_id,
                {
                    "call_id": call_id,
                    "name": tool.name,
                    "spawn_handle_id": spawn_handle_id,
                },
            )
            event_log.append(parked_env)
            try:
                if not ws.closed:
                    await ws.send_json(parked_env)
            except Exception:
                log.debug("tool_ask_parked send failed for %s", call_id, exc_info=True)
            await approval_log.record_ask(
                session_id=session_id,
                call_id=call_id,
                tool_name=tool.name,
                input_summary=approval_log.summarize_input(raw_input),
                posture_source=context.posture_source or "default",
                result="parked",
                actor="timeout",
            )
            log.info(
                "ask_fn parked (background spawn %s): call_id=%s tool=%s "
                "park_timeout_s=%s",
                spawn_handle_id, call_id, tool.name, park_timeout_s,
            )
            try:
                try:
                    decision = await asyncio.wait_for(
                        asyncio.shield(fut), timeout=park_timeout_s
                    )
                    return decision, ("allow_once" if decision else "deny")
                except asyncio.TimeoutError:
                    if fut.done() and not fut.cancelled():
                        # Settle race (W1 approvals pattern): a decision at
                        # the boundary instant is honored, not discarded.
                        decision = fut.result()
                        return decision, ("allow_once" if decision else "deny")
                    if not fut.done():
                        fut.cancel()
                    return False, "park_timeout"
            finally:
                if parked_asks is not None:
                    parked_asks.pop(entry.approval_id, None)
                if handle is not None:
                    mark_input_required(handle, False)

        try:
            try:
                # `asyncio.shield` is load-bearing: bare `wait_for(fut, ...)`
                # internally calls `fut.cancel()` when the timer fires,
                # leaving the future in a permanently-done-cancelled state.
                # The grace window's second `wait_for` would then re-await
                # the already-cancelled future and raise CancelledError
                # immediately — silently routing every normal timeout into
                # the cancel path. Shield keeps `fut` itself alive so a
                # late `set_result` from `_resolve_ask` can still land.
                approved = await asyncio.wait_for(asyncio.shield(fut), timeout=ASK_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                # Grace window — a click landed within +ASK_GRACE_SECONDS
                # still counts. Without this, clicks racing the t=30.0 timer
                # silently drop and surface as "operator declined" despite
                # the operator pressing yes a fraction of a second too late.
                try:
                    approved = await asyncio.wait_for(asyncio.shield(fut), timeout=ASK_GRACE_SECONDS)
                    log.info(
                        "ask_fn late response inside grace window: call_id=%s tool=%s approved=%s",
                        call_id, tool.name, approved,
                    )
                except asyncio.TimeoutError:
                    # Both windows expired with no operator response.
                    spawn_handle_id = _spawn_handle_id_of_current_task()
                    if spawn_handle_id is not None:
                        # trio W4 — background-spawn ASK parks instead of
                        # dying: the question moves to the approvals pane,
                        # the spawn shows input_required, the SAME future
                        # resumes the work whenever the operator answers.
                        approved, park_result = await _park_and_wait(
                            spawn_handle_id
                        )
                        timed_out = park_result == "park_timeout"
                    else:
                        # Foreground turn: deny, byte-identical to before.
                        # Explicitly cancel `fut` so a later `_resolve_ask`
                        # call doesn't `set_result` on an orphan still wired
                        # into anything that might keep it referenced.
                        if not fut.done():
                            fut.cancel()
                        approved = False
                        timed_out = True
                        log.info(
                            "ask_fn timeout: call_id=%s tool=%s",
                            call_id, tool.name,
                        )
        except asyncio.CancelledError:
            # The turn task was cancelled mid-wait (operator hit cancel,
            # WS closed via cleanup_session, etc.). Without explicit
            # handling, no tool_denied envelope ships — the UI modal stays
            # visually pinned and the audit row never gets written. Surface
            # a cancellation envelope + audit row, then re-raise so the
            # cancel propagates as expected.
            #
            # Both cleanup awaits are wrapped in `asyncio.shield` because
            # CancelledError is a BaseException (not Exception) in 3.8+, so
            # the `except Exception` guards below cannot catch a re-delivered
            # cancel that lands at the next `await` point — and the cleanup
            # would be interrupted silently, dropping the audit row.
            cancelled = True
            pending_asks.pop(call_id, None)
            if parked_asks is not None:
                # spawn_cancel on a parked spawn discards the question with
                # the work (trio W4). Entries are keyed by approval_id now
                # (M13) and `entry` isn't in this scope, so drop by matching
                # (session_id, call_id). Usually a no-op — _park_and_wait's
                # finally already removed it — but correct if it didn't.
                for aid in [
                    a for a, e in parked_asks.items()
                    if e.call_id == call_id and e.session_id == session_id
                ]:
                    parked_asks.pop(aid, None)
            if not fut.done():
                fut.cancel()
            try:
                cancel_env = make_envelope(
                    "tool_denied",
                    "execution",
                    session_id,
                    {"call_id": call_id, "reason": "turn_cancelled"},
                )
                event_log.append(cancel_env)
                if not ws.closed:
                    await asyncio.shield(ws.send_json(cancel_env))
            except Exception:
                log.debug("cancel-path tool_denied send failed for %s", call_id, exc_info=True)
            try:
                await asyncio.shield(approval_log.record_ask(
                    session_id=session_id,
                    call_id=call_id,
                    tool_name=tool.name,
                    input_summary=approval_log.summarize_input(raw_input),
                    posture_source=context.posture_source or "default",
                    result="cancelled",
                    actor="system",
                ))
            except Exception:
                log.debug("cancel-path audit-row write failed for %s", call_id, exc_info=True)
            raise
        finally:
            if not cancelled:
                pending_asks.pop(call_id, None)

        reply_env = make_envelope(
            "tool_approved" if approved else "tool_denied",
            "execution",
            session_id,
            {"call_id": call_id},
        )
        event_log.append(reply_env)
        if park_result is not None:
            # trio W4 — a parked ask may resolve hours later against a
            # closed/reconnected WS; the decision must reach the tool
            # pipeline regardless (the event_log row replays on reconnect).
            try:
                if not ws.closed:
                    await ws.send_json(reply_env)
            except Exception:
                log.debug("post-park reply send failed for %s", call_id, exc_info=True)
        else:
            await ws.send_json(reply_env)

        if park_result is not None:
            final_result = park_result
        else:
            final_result = (
                "allow_once" if approved else ("timeout" if timed_out else "deny")
            )
        await approval_log.record_ask(
            session_id=session_id,
            call_id=call_id,
            tool_name=tool.name,
            input_summary=approval_log.summarize_input(raw_input),
            posture_source=context.posture_source or "default",
            result=final_result,
            actor="timeout" if timed_out else "operator",
        )
        return approved

    return ask_fn
