"""Delegation over the lane path — the only transport `delegate_*` uses.

There used to be two delegation systems. `lane_turn` crossed IPC to the
controller daemon and got a named, durable lane with a replayable event
stream, an interrupt, and a record that survived a restart. `delegate_coder`
and `delegate_auditor` ran a local subprocess inside the Mirror backend and got
a spawn handle: no event stream, no interrupt, journaled as lost on restart.
The assistant held both and had to reason about two lifecycle and recovery models for
what looks like one act.

A delegation is now a turn on an EPHEMERAL lane: same identity, same event
stream, same interrupt, same wait primitive — closed on the way out whatever
happened. Ephemeral because a delegation is one task, not a standing
collaborator; the named lanes stay the operator's, unpolluted.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from tesseract.kernel.tools.base import CliSink, ToolContext, ToolResult
from tesseract.orchestrator.agent_controller.lanes.tool_support import maybe_await

log = logging.getLogger(__name__)

# Why a delegation's reply is delivered whole rather than pointed at: see
# `brain/chat.py::_format_spawn_completion`. TokenJuice's `delegate` family
# rules compress it on the way to the model.


class LaneDelegationUnavailable(RuntimeError):
    """The lane path could not be reached. Raised rather than degrading to a
    local subprocess: a fallback transport is a second delegation system
    wearing the first one's name."""


async def resolve_delegation_manager(context: ToolContext) -> Any:
    """The LaneManager a delegation will run on.

    A wired provider (Mirror chat sessions, the autonomy runner) wins;
    a context with none — a sub-agent, a bare script — gets the IPC proxy,
    because the daemon is the only process that hosts a real LaneManager.

    Either way, an IPC-backed manager needs a daemon to talk to, so ensure
    one before handing it back. Gating that on "no provider was wired" was
    the mistake: every caller that reaches the daemon over IPC has one
    wired, so the guarantee never fired for anybody. Unreachable is an
    error, never a quiet fallback."""
    from tesseract.orchestrator.agent_controller.lanes.ipc_proxy import (
        IpcLaneManager,
    )
    from tesseract.orchestrator.agent_controller.lanes.principals import (
        OPERATOR_PRINCIPAL,
    )
    from tesseract.orchestrator.agent_controller.lanes.tool_support import (
        resolve_lane_manager,
    )

    # The fallback proxy is for a context that wired no provider at all — a
    # delegation running on the assistant's own behalf. Anything that came in over the
    # hub arrives with a provider already bound to its client identity.
    manager = resolve_lane_manager(context) or IpcLaneManager(
        caller_principal=context.caller_principal or OPERATOR_PRINCIPAL
    )
    if not isinstance(manager, IpcLaneManager):
        # In-process manager: this IS the controller, nothing to start.
        return manager
    from tesseract.orchestrator.agent_controller import dispatcher

    try:
        alive = await dispatcher.ensure_daemon_running()
    except Exception as exc:  # noqa: BLE001 — surfaced as a tool error
        raise LaneDelegationUnavailable(
            f"the lane path is unreachable: {exc}"
        ) from exc
    if not alive:
        raise LaneDelegationUnavailable(
            "the lane path is unreachable: no controller daemon"
        )
    return manager


async def _emit(
    sink: CliSink | None,
    call_id: str,
    event: str,
    payload: dict[str, Any],
    *,
    shielded: bool = False,
) -> None:
    """Push one event to the operator's view. Never load-bearing.

    `shielded` is for the terminal event: it is emitted from a `finally`
    whose usual trigger is a cancellation, and an unshielded await there
    would be cancelled before the sink saw it — leaving the card open,
    which is the bug the finally exists to close."""
    if sink is None:
        return
    try:
        call = sink(event, call_id, payload)
        await (asyncio.shield(asyncio.ensure_future(call)) if shielded else call)
    except Exception:  # noqa: BLE001 — the operator's view is never load-bearing
        log.debug("delegate: cli_sink %s failed", event, exc_info=True)
    except asyncio.CancelledError:
        # The shielded emit is already on its way; don't let the card's
        # terminal event swallow the cancellation itself.
        raise


def render_event(event: Any) -> str:
    """One lane event as the line the operator would have seen scroll past.

    The DelegateCard used to be fed raw subprocess bytes. Lane events carry
    the same story in typed form, so this renders rather than replays —
    which is also why tool calls stay visible instead of being flattened
    into whatever the CLI happened to print."""
    payload = event.payload
    if event.kind == "assistant_text":
        return str(payload.get("text") or "")
    if event.kind == "tool_use":
        return f"· {payload.get('name') or 'tool'}"
    if event.kind == "tool_result":
        return " (error)" if payload.get("is_error") else ""
    if event.kind == "error":
        return f"error: {payload.get('message') or payload.get('result') or ''}"
    return ""


async def _close_lane(manager: Any, lane_id: str) -> None:
    """Give the ephemeral lane back.

    Shielded because the common reason this runs is that the delegation was
    cancelled — an unshielded close would be cancelled in turn and strand a
    lane holding a live CLI subprocess. Shielding alone is not enough: the
    shield re-raises immediately on cancellation and leaves the close merely
    STARTED, so a teardown that stops pumping the loop finishes without it.
    The cancelled path awaits the task to completion before re-raising."""
    task = asyncio.ensure_future(
        maybe_await(manager.close(lane_id, "delegate_complete"))
    )
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:  # noqa: BLE001 — best-effort; the janitor is a backstop
            log.warning(
                "delegate: closing ephemeral lane %s failed after cancel",
                lane_id, exc_info=True,
            )
        raise
    except Exception:  # noqa: BLE001 — best-effort; the janitor is the backstop
        log.warning("delegate: closing ephemeral lane %s failed", lane_id,
                    exc_info=True)


async def run_delegation_on_lane(
    *,
    tool_name: str,
    cli_label: str,
    kind: str,
    model: str,
    task: str,
    timeout_s: float,
    working_dir: str,
    context: ToolContext,
    lane_ref: dict | None = None,
) -> ToolResult:
    """Open an ephemeral lane, run one turn on it, close it.

    `timeout_s` bounds SILENCE, matching every other lane wait — a
    wall-clock cap on total duration abandons healthy long-running turns
    (2026-07-13). A delegate that keeps emitting keeps running; one that
    goes quiet is reported stalled with whatever it had produced."""
    from tesseract.config.cockpit import load_conductor_relay

    manager = await resolve_delegation_manager(context)
    poll_s, _ = load_conductor_relay()
    sink = context.cli_sink
    call_id = context.current_call_id or ""
    if not call_id:
        sink = None

    lane_id = await maybe_await(
        manager.open(kind=kind, model=model, working_dir=working_dir)
    )
    if lane_ref is not None:
        # Published for `make_delegate_cancel`: a background delegation's
        # handle is registered before its lane exists, so the cancel path
        # learns the lane here.
        lane_ref["manager"] = manager
        lane_ref["lane_id"] = lane_id
    # Everything after the open is inside the cleanup, including the
    # cli_start emit: a cancellation landing on that await is a
    # CancelledError, which is a BaseException, so an emit outside the try
    # would carry it past the close and strand the lane it just opened.
    turn_id: str | None = None
    outcome: Any = None
    try:
        try:
            await _emit(
                sink,
                call_id,
                "cli_start",
                {"tool": tool_name, "argv": [cli_label, kind]},
            )
            send_result = await maybe_await(manager.send(lane_id, task))
            if not send_result.accepted:
                return ToolResult(
                    output=(
                        f"{tool_name} rejected by its lane: "
                        f"{send_result.reason or 'unspecified'}"
                    ),
                    is_error=True,
                    metadata={"lane_id": lane_id},
                )
            turn_id = send_result.turn_id
            if not turn_id:
                return ToolResult(
                    output=(
                        f"{tool_name} failed: the lane accepted the task "
                        f"without naming a turn, so this call cannot wait "
                        f"for its own reply"
                    ),
                    is_error=True,
                    metadata={"lane_id": lane_id},
                )

            async def _tap(events: list[Any]) -> None:
                for event in events:
                    rendered = render_event(event)
                    if rendered:
                        await _emit(
                            sink,
                            call_id,
                            "cli_output",
                            {"tool": tool_name, "delta": rendered + "\n"},
                        )

            outcome = await maybe_await(
                manager.await_turn(
                    lane_id,
                    turn_id,
                    timeout=timeout_s,
                    poll_s=poll_s,
                    on_events=_tap if sink is not None else None,
                )
            )
        finally:
            # Paired with cli_start: every path that opened the card closes
            # it. The subprocess transport this replaced emitted its terminal
            # event on every exit including a failure to spawn; without this
            # a rejected or crashed delegation left the DelegateCard
            # streaming forever for a call that had already returned.
            await _emit(
                sink,
                call_id,
                "cli_end",
                {
                    "tool": tool_name,
                    "exit_code": (
                        0
                        if (outcome is not None
                            and outcome.completed
                            and not outcome.is_error)
                        else 1
                    ),
                    "stderr": (outcome.error or "") if outcome is not None else "",
                },
                shielded=True,
            )
    finally:
        await _close_lane(manager, lane_id)

    metadata = {
        "tool": tool_name,
        "lane_id": lane_id,
        "turn_id": turn_id,
        "turn_completed": outcome.completed,
        "cursor": outcome.cursor,
    }
    if not outcome.completed:
        body = outcome.reply_text or f"({cli_label} produced no output)"
        return ToolResult(
            output=(
                f"{body}\n\n[{tool_name} stalled — no lane activity for "
                f"{timeout_s:.0f}s; the partial reply above is everything it "
                f"emitted]"
            ),
            is_error=True,
            timed_out=True,
            metadata=metadata,
        )
    if outcome.is_error:
        detail = outcome.error or "the CLI reported a failed turn"
        body = outcome.reply_text or ""
        return ToolResult(
            output=f"{body}\n\n[{tool_name} failed: {detail}]".strip(),
            is_error=True,
            metadata=metadata,
        )
    if not outcome.reply_text:
        return ToolResult(
            output=f"{cli_label} returned empty output",
            is_error=True,
            metadata=metadata,
        )
    return ToolResult(output=outcome.reply_text, metadata=metadata)


# Interrupt tasks are held so asyncio's weak reference to a running task
# cannot collect one mid-flight — the same reason `lane_turn` holds its own.
_CANCELS: set[asyncio.Task] = set()


def make_delegate_cancel(lane_ref: dict):
    """A `cancel_fn` that interrupts a background delegation's lane.

    Unscoped by turn on purpose, and safe here in a way it is not for
    `lane_turn`: an ephemeral lane hosts exactly one turn, so "whatever this
    lane is running" and "this delegation" are the same turn. Before the lane
    exists there is nothing to reach for and the registry's task cancel is
    the whole cancel."""

    def _cancel() -> None:
        manager = lane_ref.get("manager")
        lane_id = lane_ref.get("lane_id")
        if manager is None or not lane_id:
            return
        try:
            task = asyncio.ensure_future(maybe_await(manager.interrupt(lane_id)))
        except RuntimeError:  # no running loop — nothing to interrupt onto
            log.debug("delegate: no loop to interrupt lane %s", lane_id)
            return
        _CANCELS.add(task)
        task.add_done_callback(_CANCELS.discard)

    return _cancel


__all__ = [
    "LaneDelegationUnavailable",
    "make_delegate_cancel",
    "render_event",
    "resolve_delegation_manager",
    "run_delegation_on_lane",
]
