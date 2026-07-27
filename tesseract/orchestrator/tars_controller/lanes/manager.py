"""`LaneManager` — six-method `lane.*` contract from
`_shared/lane-contract.md` v1.

`assistant_text` and `tool_result` are DISTINCT event kinds — never
conflate them at the wire (audit-2026-05-24 Critical regression guard).
Each lane owns one `asyncio.Lock`; cross-lane work runs concurrently."""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from tesseract.config.mcp import load_mcp_config
from tesseract.orchestrator.activity.hooks import register_lane, update_lane_state

from . import mcp_provision
from .events_log import (
    LaneEventsCursor,
    append_event,
    read_events_since,
)
from .models import (
    Lane,
    LaneEvent,
    LaneKind,
    LaneLifecycle,
    LaneMode,
    LaneSendResult,
    LaneSnapshot,
    LaneStatus,
)
from .store import (
    archive_lane,
    lane_dir,
    list_lane_ids,
    read_lane,
    write_lane,
)

log = logging.getLogger(__name__)


class LaneManagerError(Exception):
    """Base for lane manager errors so callers can catch with one type."""


class LaneNotFoundError(LaneManagerError):
    """Raised on operations against an unknown `lane_id`."""


class LaneAdapter(Protocol):
    """Transport-level driver for one lane.

    `run_turn` emits each adapter event via `on_event` and returns a
    result dict ``{"session_id", "is_error", "usage"}`` when the turn
    completes. Tests inject stubs; production uses
    `ClaudeStreamAdapter` / `CodexStreamAdapter` wrappers."""

    async def run_turn(
        self,
        *,
        message: str,
        on_event: Callable[[dict[str, Any]], None],
        cancel_event: asyncio.Event | None,
    ) -> dict[str, Any]:
        ...


AdapterFactory = Callable[[Lane, "LaneRuntime"], LaneAdapter]
"""Returns the adapter that will drive a lane's turns."""


def _mint_lane_id(kind: LaneKind) -> str:
    return f"lane-{kind}-{secrets.token_hex(6)}"


@dataclass
class LaneRuntime:
    """Per-lane mutable state the manager owns. Not persisted — recovered
    on `attach` by re-reading `lane.json` + tailing `events.jsonl`."""

    lane: Lane
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    busy: bool = False
    queue_depth: int = 0
    last_activity_utc: str = ""
    current_turn_id: str | None = None
    end_of_turn_at_utc: str | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    turn_tasks: set[asyncio.Task] = field(default_factory=set)


class LaneManager:
    """Owner of every lane the controller daemon hosts.

    The manager is stateless w.r.t. the brain — every lane's authority
    lives in `lane.json` + `events.jsonl` on disk. The in-memory
    `LaneRuntime` map is a cache that the manager rebuilds on `attach`
    after a daemon restart."""

    def __init__(
        self,
        *,
        adapter_factory: AdapterFactory | None = None,
        root: Path | None = None,
    ) -> None:
        # `root` override is for tests; production resolves at call time
        # from `<TESSERACT_HOME>/controller/lanes/` via store.py helpers.
        self._root_override = root
        self._adapter_factory = adapter_factory or _default_adapter_factory
        self._runtimes: dict[str, LaneRuntime] = {}
        # Headless adapters are stateless; caching them is benign.
        self._adapters: dict[str, LaneAdapter] = {}

    # ------------------------------------------------------------------ paths

    def _lane_dir(self, lane_id: str) -> Path:
        if self._root_override is not None:
            return self._root_override / lane_id
        return lane_dir(lane_id)

    def _events_path(self, lane_id: str) -> Path:
        return self._lane_dir(lane_id) / "events.jsonl"

    def _transcript_path(self, lane_id: str) -> Path:
        return self._lane_dir(lane_id) / "transcript.txt"

    def _last_cursor_path(self, lane_id: str) -> Path:
        return self._lane_dir(lane_id) / "last_cursor.txt"

    # ------------------------------------------------------------------ open

    async def open(
        self,
        *,
        kind: LaneKind,
        mode: LaneMode = "headless",
        model: str,
        working_dir: str,
        env: dict[str, str] | None = None,
    ) -> str:
        """Create a new lane. Returns the lane id; status starts at
        ``spawning`` and flips to ``ready`` once the runtime cache is
        populated. `headless` is the only mode wired."""
        lane_id = _mint_lane_id(kind)
        lane = Lane(
            lane_id=lane_id,
            kind=kind,
            mode=mode,
            model=model,
            working_dir=working_dir,
            env=dict(env or {}),
            lifecycle="ready",
        )
        write_lane(lane)
        runtime = LaneRuntime(lane=lane)
        self._runtimes[lane_id] = runtime
        # Always-on opening event so a cold reader sees the lane existed.
        self._append(
            lane_id,
            LaneEvent(
                lane_id=lane_id,
                kind="status_change",
                payload={"lifecycle": "ready", "kind": kind, "mode": mode},
            ),
        )
        # AS-1 — project the lane into the activity registry. Bare label (the
        # lane id); NamedLaneManager.ensure upserts the human name on top.
        register_lane(lane_id, label=lane_id, provider=kind, lifecycle="ready")
        return lane_id

    # ------------------------------------------------------------------ send

    async def send(self, lane_id: str, message: str) -> LaneSendResult:
        """Fire-and-queue: accept the message, run the turn on a
        background task, return immediately.

        The ack means "queued", never "completed" — completion is the
        lane's `turn_ended` event (every accepted turn is guaranteed one,
        even when the adapter raises). This mirrors the CLI-agent
        contract: submit → immediate accept → stream events → terminal
        result. `queue_depth` in the result includes the turn just
        queued; it decrements when the turn acquires the lane lock."""
        runtime = self._require_runtime(lane_id)
        if runtime.lane.lifecycle in ("closed", "closing"):
            return LaneSendResult(
                accepted=False,
                queue_depth=runtime.queue_depth,
                reason=f"lane is {runtime.lane.lifecycle}",
            )
        runtime.queue_depth += 1
        task = asyncio.create_task(self._run_queued_turn(runtime, message))
        runtime.turn_tasks.add(task)
        task.add_done_callback(runtime.turn_tasks.discard)
        return LaneSendResult(accepted=True, queue_depth=runtime.queue_depth)

    async def send_and_await(
        self,
        lane_id: str,
        message: str,
        *,
        timeout: float,
        poll_s: float = 0.5,
    ) -> LaneSendResult:
        """Send then poll read() until this turn's `turn_ended` arrives.
        Same contract as `IpcLaneManager.send_and_await` so
        `lane_send(wait=True)` blocks identically in-process and over IPC.
        `timeout` bounds SILENCE (stall), not total turn duration — lane
        activity extends the wait (2026-07-13 incident: wall-clock caps
        abandoned healthy long turns). On stall, returns the send result —
        caller reads the remaining events via lane_read."""
        _, cursor = self.read(lane_id, None)  # capture current tail
        result = await self.send(lane_id, message)
        if not result.accepted:
            return result
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            events, cursor = self.read(lane_id, cursor)
            if events:
                deadline = loop.time() + timeout
            if any(e.kind == "turn_ended" for e in events):
                return result
            await asyncio.sleep(poll_s)
        return result

    async def drain(self, lane_id: str) -> None:
        """Await every queued/in-flight turn task for the lane. Test and
        shutdown helper — production waiters follow the `turn_ended`
        event stream instead."""
        runtime = self._runtimes.get(lane_id)
        if runtime is None:
            return
        tasks = list(runtime.turn_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_queued_turn(self, runtime: LaneRuntime, message: str) -> None:
        lane_id = runtime.lane.lane_id
        decremented = False
        try:
            async with runtime.lock:
                runtime.queue_depth -= 1
                decremented = True
                if runtime.lane.lifecycle in ("closed", "closing"):
                    log.debug(
                        "lane.send: dropping queued turn for %s lane",
                        runtime.lane.lifecycle,
                    )
                    return
                await self._run_one_turn(runtime, message)
        except asyncio.CancelledError:
            if not decremented:
                runtime.queue_depth -= 1
            raise
        except Exception:  # noqa: BLE001 — logged; events carry the error
            # _run_one_turn already appended the error + turn_ended events;
            # nothing awaits this task, so swallow after logging.
            log.exception("lane.send: turn raised for %s", lane_id)

    async def interrupt(self, lane_id: str) -> bool:
        """M2 — cancel the lane's in-flight turn (kills the CLI subprocess via
        cancel_event) WITHOUT closing the lane, so a follow-up send runs
        immediately. Returns True if a turn was interrupted, False if idle or
        unknown. This is the 'cancel + resend' steer path for a busy lane: the
        stale work is abandoned and the correction runs now."""
        runtime = self._runtimes.get(lane_id)
        if runtime is None or not runtime.busy:
            return False
        # Fire ONLY the current turn's cancel event. Each turn gets its own
        # event (_run_one_turn), so a turn already queued behind the lock is
        # unaffected — no stale-event kill (audit M2 review). The interrupted
        # turn observes this immediately (cli_adapter races readline vs cancel)
        # and releases the lane lock; the follow-up send runs the correction.
        runtime.cancel_event.set()
        return True

    async def _run_one_turn(self, runtime: LaneRuntime, message: str) -> None:
        lane = runtime.lane
        runtime.busy = True
        # M2 — a fresh cancel event per turn so interrupt() aborts only THIS
        # turn; a subsequently-queued turn gets its own and can't be killed by
        # a stale set() from the turn it was queued behind.
        runtime.cancel_event = asyncio.Event()
        update_lane_state(lane.lane_id, "busy")  # AS-1 — running pulse
        turn_id = secrets.token_hex(4)
        runtime.current_turn_id = turn_id
        self._append(
            lane.lane_id,
            LaneEvent(
                lane_id=lane.lane_id,
                kind="turn_started",
                payload={"turn_id": turn_id, "message": message},
            ),
        )
        adapter = self._adapters.get(lane.lane_id)
        if adapter is None:
            adapter = self._adapter_factory(lane, runtime)
            self._adapters[lane.lane_id] = adapter

        def _on_adapter_event(raw: dict[str, Any]) -> None:
            for translated in _translate_adapter_event(
                lane.kind, lane.lane_id, raw
            ):
                self._append(lane.lane_id, translated)

        try:
            result = await adapter.run_turn(
                message=message,
                on_event=_on_adapter_event,
                cancel_event=runtime.cancel_event,
            )
        except Exception as exc:
            # Completion contract: every turn_started gets a turn_ended,
            # even when the adapter raises. Stream waiters (lane_turn,
            # send_and_await) key off turn_ended and must never hang on
            # a crashed turn.
            self._append(
                lane.lane_id,
                LaneEvent(
                    lane_id=lane.lane_id,
                    kind="error",
                    payload={"message": str(exc), "phase": "turn"},
                ),
            )
            end_event = LaneEvent(
                lane_id=lane.lane_id,
                kind="turn_ended",
                payload={
                    "turn_id": turn_id,
                    "is_error": True,
                    "error": str(exc),
                    "usage": {},
                },
            )
            self._append(lane.lane_id, end_event)
            runtime.end_of_turn_at_utc = end_event.at_utc
            raise
        finally:
            runtime.busy = False
            # Reset here, not after the post-processing below: a raising
            # run_turn (e.g. provisioning failure) otherwise leaves a stale
            # turn id in lane status while busy=False (Deferred 2026-07-12).
            runtime.current_turn_id = None
            update_lane_state(lane.lane_id, "ready")  # AS-1 — back to idle
        new_session_id = result.get("session_id") if isinstance(result, dict) else None
        if isinstance(new_session_id, str) and new_session_id:
            if lane.cli_session_id != new_session_id:
                lane.cli_session_id = new_session_id
                write_lane(lane)
        is_error = bool(result.get("is_error")) if isinstance(result, dict) else False
        usage = result.get("usage") if isinstance(result, dict) else {}
        end_event = LaneEvent(
            lane_id=lane.lane_id,
            kind="turn_ended",
            payload={
                "turn_id": turn_id,
                "is_error": is_error,
                "usage": usage if isinstance(usage, dict) else {},
            },
        )
        self._append(lane.lane_id, end_event)
        runtime.end_of_turn_at_utc = end_event.at_utc
        runtime.last_activity_utc = end_event.at_utc

    # ------------------------------------------------------------------ read

    def read(
        self, lane_id: str, since_cursor: str | None = None
    ) -> tuple[list[LaneEvent], str]:
        """Pull events since `since_cursor`. Returns `(events,
        next_cursor)`. Idempotent: same cursor → same events until a new
        append lands."""
        self._require_lane_exists(lane_id)
        cursor = LaneEventsCursor.parse(since_cursor)
        events, next_cursor = read_events_since(self._events_path(lane_id), cursor)
        # Advisory: record the last cursor any reader saw. Not authoritative
        # (the byte offset is); useful for human eyeballing of where a
        # client got to. Failure is non-fatal — read MUST succeed even if
        # the disk is read-only or the file is locked.
        try:
            self._last_cursor_path(lane_id).write_text(
                next_cursor.wire, encoding="utf-8"
            )
        except OSError:
            log.debug(
                "lane %s: last_cursor write failed", lane_id, exc_info=True
            )
        return events, next_cursor.wire

    # ------------------------------------------------------------------ status

    def status(self, lane_id: str) -> LaneStatus:
        runtime = self._runtimes.get(lane_id)
        if runtime is None:
            lane = read_lane(lane_id)
            return LaneStatus(
                alive=False,
                busy=False,
                queue_depth=0,
                last_activity_utc=lane.closed_at_utc or lane.opened_at_utc,
                lifecycle=lane.lifecycle,
            )
        return LaneStatus(
            alive=runtime.lane.lifecycle not in ("closed", "error"),
            busy=runtime.busy,
            queue_depth=runtime.queue_depth,
            last_activity_utc=runtime.last_activity_utc
            or runtime.lane.opened_at_utc,
            current_turn_id=runtime.current_turn_id,
            end_of_turn_at_utc=runtime.end_of_turn_at_utc,
            lifecycle=runtime.lane.lifecycle,
        )

    # ------------------------------------------------------------------ attach

    async def attach(self, lane_id: str) -> LaneSnapshot:
        """Re-establish visibility on a lane. After a brain restart the
        manager-side runtime cache is empty; this rebuilds it from
        `lane.json` and tails `events.jsonl` from offset 0 so the caller
        has the full history. After a daemon restart the underlying
        process is gone — the lane's `lifecycle` already reflects that
        from a prior `close`, or the caller can re-`open` a fresh lane."""
        lane = read_lane(lane_id)
        runtime = self._runtimes.get(lane_id)
        if runtime is None:
            runtime = LaneRuntime(lane=lane)
            self._runtimes[lane_id] = runtime
        events, next_cursor = read_events_since(
            self._events_path(lane_id), LaneEventsCursor(0)
        )
        status = self.status(lane_id)
        return LaneSnapshot(
            lane=lane,
            status=status,
            recent_events=events,
            next_cursor=next_cursor.wire,
        )

    # ------------------------------------------------------------------ close

    async def close(self, lane_id: str, reason: str) -> dict[str, Any]:
        """Terminate the lane.

        Sets `cancel_event` to interrupt any active turn, calls the
        adapter's `close()` (SIGTERM → 5 s grace → SIGKILL for PTY;
        no-op for headless), then marks the on-disk record closed,
        emits the `closed` event, and archives the directory."""
        runtime = self._runtimes.get(lane_id)
        lane = runtime.lane if runtime is not None else read_lane(lane_id)
        if runtime is not None:
            runtime.cancel_event.set()
            runtime.lane.lifecycle = "closing"
            # Settle queued/in-flight turn tasks BEFORE the closed event +
            # archive move — a straggler appending afterwards would
            # recreate the live lane dir next to the archive.
            pending = [t for t in runtime.turn_tasks if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        adapter = self._adapters.pop(lane_id, None)
        if adapter is not None:
            adapter_close = getattr(adapter, "close", None)
            if callable(adapter_close):
                try:
                    await adapter_close()
                except Exception:  # noqa: BLE001 — best-effort close
                    log.exception(
                        "lane %s: adapter.close() raised", lane_id
                    )
        closed_at = datetime.now(timezone.utc).isoformat()
        lane.lifecycle = "closed"
        lane.closed_at_utc = closed_at
        lane.close_reason = reason
        write_lane(lane)
        self._append(
            lane_id,
            LaneEvent(
                lane_id=lane_id,
                kind="closed",
                payload={"reason": reason, "closed_at_utc": closed_at},
            ),
        )
        update_lane_state(lane_id, "closed")  # AS-1 — terminal transition
        # Archive after the closed event lands so the events.jsonl row
        # describing the close moves with the rest of the lane payload.
        dest = archive_lane(lane_id)
        self._runtimes.pop(lane_id, None)
        return {
            "transcript_path": str(dest / "transcript.txt"),
            "final_status": "closed",
            "archived_at_utc": closed_at,
            "archive_dir": str(dest),
        }

    # ------------------------------------------------------------------ list

    def list_ids(self) -> list[str]:
        """All live lane ids (on-disk + cached). Archived lanes are not
        included; consumers query the archive directly if they want
        historical state."""
        return list_lane_ids()

    # ------------------------------------------------------------------ helpers

    def _require_runtime(self, lane_id: str) -> LaneRuntime:
        runtime = self._runtimes.get(lane_id)
        if runtime is None:
            raise LaneNotFoundError(
                f"lane {lane_id} is not attached; call attach() first"
            )
        return runtime

    def _require_lane_exists(self, lane_id: str) -> None:
        if lane_id in self._runtimes:
            return
        if not (self._lane_dir(lane_id) / "lane.json").exists():
            raise LaneNotFoundError(f"unknown lane {lane_id}")

    def _append(self, lane_id: str, event: LaneEvent) -> None:
        append_event(self._events_path(lane_id), event)
        runtime = self._runtimes.get(lane_id)
        if runtime is not None:
            runtime.last_activity_utc = event.at_utc
        self._append_transcript(lane_id, event)

    def _append_transcript(self, lane_id: str, event: LaneEvent) -> None:
        """Append a model-side prose line to `transcript.txt` for the
        events a human reader would care about — assistant text, tool
        calls, tool results, errors, close. Non-renderable kinds
        (status_change, turn_started, turn_ended) are skipped.

        Best-effort: failures are swallowed so `_append` (the load-bearing
        events.jsonl path) is never blocked by a transcript write error."""
        line: str | None
        kind = event.kind
        payload = event.payload
        if kind == "assistant_text":
            text = str(payload.get("text") or "").strip()
            line = f"assistant: {text}" if text else None
        elif kind == "tool_use":
            name = payload.get("name") or "<tool>"
            tool_use_id = payload.get("tool_use_id") or ""
            line = f"tool_use[{tool_use_id}] {name}: {payload.get('input')}"
        elif kind == "tool_result":
            tool_use_id = payload.get("tool_use_id") or ""
            output = payload.get("output")
            is_error = " (error)" if payload.get("is_error") else ""
            line = f"tool_result[{tool_use_id}]{is_error}: {output}"
        elif kind == "error":
            msg = payload.get("message") or payload.get("result") or ""
            line = f"error: {msg}"
        elif kind == "closed":
            line = f"closed: {payload.get('reason') or ''}"
        else:
            line = None
        if line is None:
            return
        try:
            with self._transcript_path(lane_id).open("a", encoding="utf-8") as fh:
                fh.write(line.rstrip() + "\n")
        except OSError:
            # Source-of-truth is events.jsonl (already appended); the
            # human-readable transcript is best-effort. Disk-full /
            # locked-file would otherwise be invisible until the
            # operator notices an empty transcript.
            log.debug("lane %s: transcript write failed", lane_id, exc_info=True)


# ---------------------------------------------------------------- translators


def _translate_adapter_event(
    kind: LaneKind, lane_id: str, raw: dict[str, Any]
) -> list[LaneEvent]:
    """Convert one transport-level adapter event into zero, one, or many
    typed LaneEvents.

    A single Claude assistant message can carry BOTH text blocks and
    tool_use blocks; the contract requires emitting them as separate
    LaneEvents (audit-2026-05-24 Critical regression guard — neither
    conflate them into one event NOR drop one in favor of the other).
    Returning a list makes that explicit. Empty list = nothing to log."""
    etype = raw.get("type")
    if kind == "claude":
        if etype == "assistant":
            msg = raw.get("message") or {}
            blocks = msg.get("content") or []
            text_chunks: list[str] = []
            tool_uses: list[dict[str, Any]] = []
            if isinstance(blocks, list):
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text_chunks.append(str(block.get("text") or ""))
                    elif btype == "tool_use":
                        tool_uses.append({
                            "tool_use_id": block.get("id"),
                            "name": block.get("name"),
                            "input": block.get("input"),
                        })
            out: list[LaneEvent] = []
            text = "".join(text_chunks).strip()
            if text:
                out.append(
                    LaneEvent(
                        lane_id=lane_id,
                        kind="assistant_text",
                        payload={"text": text},
                    )
                )
            for tu in tool_uses:
                out.append(
                    LaneEvent(lane_id=lane_id, kind="tool_use", payload=tu)
                )
            return out
        if etype == "user":
            # Claude emits tool_result wrapped inside a user message; a
            # single user message can carry multiple tool_result blocks
            # when several tools fan out in one turn.
            msg = raw.get("message") or {}
            blocks = msg.get("content") or []
            out = []
            if isinstance(blocks, list):
                for block in blocks:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_result"
                    ):
                        out.append(
                            LaneEvent(
                                lane_id=lane_id,
                                kind="tool_result",
                                payload={
                                    "tool_use_id": block.get("tool_use_id"),
                                    "output": block.get("content"),
                                    "is_error": bool(block.get("is_error")),
                                },
                            )
                        )
            return out
        if etype == "system" and raw.get("subtype") == "init":
            sid = raw.get("session_id")
            if isinstance(sid, str):
                return [
                    LaneEvent(
                        lane_id=lane_id,
                        kind="status_change",
                        payload={"cli_session_id": sid},
                    )
                ]
        if etype == "result":
            subtype = raw.get("subtype") or ""
            if isinstance(subtype, str) and subtype.startswith("error"):
                return [
                    LaneEvent(
                        lane_id=lane_id,
                        kind="error",
                        payload={"subtype": subtype, "result": raw.get("result")},
                    )
                ]
            return []
    elif kind == "codex":
        if etype == "thread.started":
            tid = raw.get("thread_id")
            if isinstance(tid, str):
                return [
                    LaneEvent(
                        lane_id=lane_id,
                        kind="status_change",
                        payload={"cli_session_id": tid},
                    )
                ]
        if etype == "item.completed":
            item = raw.get("item") or {}
            if not isinstance(item, dict):
                return []
            itype = item.get("type")
            if itype == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text:
                    return [
                        LaneEvent(
                            lane_id=lane_id,
                            kind="assistant_text",
                            payload={"text": text},
                        )
                    ]
            elif itype == "command_execution":
                # Codex fuses tool_use + tool_result in one `item.completed`;
                # split them back into two distinct LaneEvents so the
                # contract's distinct-kind guarantee holds for Codex too.
                tool_use_id = item.get("id") or item.get("call_id") or ""
                return [
                    LaneEvent(
                        lane_id=lane_id,
                        kind="tool_use",
                        payload={
                            "tool_use_id": tool_use_id,
                            "name": "command_execution",
                            "input": {"command": item.get("command")},
                        },
                    ),
                    LaneEvent(
                        lane_id=lane_id,
                        kind="tool_result",
                        payload={
                            "tool_use_id": tool_use_id,
                            "output": item.get("result"),
                            "is_error": bool(item.get("is_error")),
                        },
                    ),
                ]
        if etype in ("error", "turn.failed"):
            return [
                LaneEvent(
                    lane_id=lane_id,
                    kind="error",
                    payload={"message": raw.get("message") or raw.get("error")},
                )
            ]
    return []


# --------------------------------------------------- default adapter factory


def _default_adapter_factory(lane: Lane, runtime: LaneRuntime) -> LaneAdapter:
    """Build the production adapter for `lane`.

    X-4 Session D introduced a `pty` transport alongside `headless`;
    the P4 PTY prune (2026-07-04) retired it — `headless` (subprocess +
    stream-JSON) is the only wired mode."""
    if lane.mode == "headless":
        from tesseract.orchestrator.tars_controller.interactive.cli_adapter import (
            ClaudeStreamAdapter,
            CodexStreamAdapter,
        )

        if lane.kind == "claude":
            base = ClaudeStreamAdapter(model=lane.model)
        elif lane.kind == "codex":
            base = CodexStreamAdapter(model=lane.model)
        else:  # pragma: no cover — Literal narrows; defensive only
            raise LaneManagerError(f"unknown lane kind {lane.kind!r}")
        return _HeadlessCliLaneAdapter(base=base, lane=lane)
    raise LaneManagerError(f"unknown lane mode {lane.mode!r}")


@dataclass
class _HeadlessCliLaneAdapter:
    """Adapts `ClaudeStreamAdapter` / `CodexStreamAdapter` to the
    `LaneAdapter` Protocol. Threads `lane.cli_session_id` so post-X-3
    multi-turn semantics route to `--resume <id>` on every send."""

    base: Any  # ClaudeStreamAdapter | CodexStreamAdapter
    lane: Lane
    _mcp_provisioned: bool = field(default=False, init=False, repr=False)

    async def run_turn(
        self,
        *,
        message: str,
        on_event: Callable[[dict[str, Any]], None],
        cancel_event: asyncio.Event | None,
    ) -> dict[str, Any]:
        if not self._mcp_provisioned:
            # P2 Task 2 — wire the CLI to the embedded MCP hub before its
            # first turn. Config-as-authority: a missing token env var
            # raises here, not on the CLI's own connect attempt.
            await asyncio.to_thread(
                mcp_provision.provision,
                Path(self.lane.working_dir),
                self.lane.kind,
                load_mcp_config(),
            )
            self._mcp_provisioned = True
        accumulator = await self.base.run_turn(
            task=message,
            session_id=self.lane.cli_session_id,
            cwd=self.lane.working_dir,
            on_event=on_event,
            cancel_event=cancel_event,
        )
        return {
            "session_id": getattr(accumulator, "session_id", None),
            "is_error": getattr(accumulator, "is_error", False),
            "usage": getattr(accumulator, "usage", {}),
            "result_text": getattr(accumulator, "result_text", ""),
        }


__all__ = [
    "AdapterFactory",
    "LaneAdapter",
    "LaneManager",
    "LaneManagerError",
    "LaneNotFoundError",
    "LaneRuntime",
]
