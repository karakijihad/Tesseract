"""lane_turn — send a message, await THAT turn's end, return its reply.

One call replaces the send -> poll lane_read -> parse dance. `name_or_id`
resolves through the named-lane binding layer first (falling back to a raw
lane_id when no binding exists), then waits via `await_turn` on the
`turn_id` its own send returned — through `maybe_await`, so it works
against both the in-process LaneManager and the Mirror IPC proxy.

Correlation is by turn id, not by position in the stream. Two overlapping
lane_turn calls on one lane used to read the same pre-send cursor and both
stop at the first `turn_ended`, so the second returned the first's reply
while its own turn ran on unobserved."""

from __future__ import annotations

import asyncio
import logging
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.config.cockpit import load_conductor_relay, load_conductor_reply_cap
from tesseract.config.runtime_limits import (
    default_runtime_config_path,
    load_max_foreground_delegate_timeout_s,
)
from tesseract.kernel.tools.base import (
    SpawnCapExceeded,
    Tool,
    ToolContext,
    ToolResult,
    spawn_cap_tool_result,
)
from tesseract.orchestrator.agent_controller.lanes.tool_support import (
    maybe_await,
    resolve_lane_manager,
    resolve_named_lane_manager,
)


log = logging.getLogger(__name__)

# `SpawnRegistry.cancel_fn` is synchronous but `interrupt` is not, so the
# interrupt runs as a task. Held here because asyncio keeps only a weak
# reference to running tasks — an unreferenced one can be collected mid-flight,
# which would make cancel silently do nothing again, just less obviously.
_INTERRUPTS: set[asyncio.Task] = set()


def _interrupt_lane(manager, lane_id: str, turn_ref: dict[str, str | None]):
    """A `cancel_fn` that stops THIS turn, not just the wait on it.

    `turn_ref` is filled in by `_run_foreground` once the send names the
    turn, because the handle is registered before the send happens. Scoping
    matters: `interrupt(lane_id)` alone cancels whichever turn is busy, so
    cancelling a queued B killed the running A and let B run on unobserved —
    the same misattribution this phase removed from the wait path."""

    def _cancel() -> None:
        turn_id = turn_ref.get("turn_id")
        if turn_id is None:
            # Cancelled before the lane named the turn. The registry cancels
            # the task itself; there is nothing on the lane to reach for, and
            # a lane-wide interrupt here would hit a sibling.
            return
        try:
            task = asyncio.ensure_future(
                maybe_await(manager.interrupt(lane_id, turn_id))
            )
        except RuntimeError:  # no running loop — nothing to interrupt onto
            log.debug("lane_turn: no loop to interrupt lane %s", lane_id)
            return
        _INTERRUPTS.add(task)
        task.add_done_callback(_INTERRUPTS.discard)

    return _cancel


class LaneTurnInput(BaseModel):
    name_or_id: str = Field(
        description="Named lane (e.g. 'coder/claude') or a raw lane_id."
    )
    message: str = Field(description="Task / instruction for the lane.")
    timeout_s: float | None = Field(
        default=None,
        description=(
            "Stall ceiling: give up only after this many seconds with NO "
            "new lane events (activity extends the wait). Overrides "
            "cockpit.yaml conductor.relay_timeout_s for this turn."
        ),
    )
    background: bool = Field(
        default=True,
        description=(
            "Dispatch and reconcile (default): send the message, return a "
            "spawn_handle immediately; the lane's reply reaches you as a "
            "completion note, or via spawn_check / spawn_await. Right for "
            "fan-out and long jobs — you stay free to answer the operator "
            "and dispatch more work meanwhile. Pass false to await inline, "
            "and only when the very next step in THIS turn consumes the "
            "reply: that blocks the entire turn for the lane's duration, "
            "the operator's queued messages included, so it is capped at "
            "runtime.yaml::max_foreground_delegate_timeout_s and a longer "
            "timeout_s is dispatched instead."
        ),
    )


class LaneTurnTool(Tool):
    default_posture = "ask"
    risk_class: ClassVar[str] = "operator_gate"

    # A CLI's reply is whatever it read out of a repository, a web page, or
    # a compromised model — untrusted by origin, however trusted the tool
    # that fetched it. Set here so the foreground result and `spawn_await`'s
    # retrieval get the same envelope the background completion delivery
    # applies in `chat.py::_format_spawn_completion`; wrapping only the one
    # path left the other two open.
    untrusted_source: ClassVar[bool] = True

    group: ClassVar[str] = "long-running-collaborators"
    summary: ClassVar[str] = "Send a message into a lane and wait for that one turn's reply, in one call."
    use_when: ClassVar[str] = (
        "Use as the default way to drive a lane collaborator: one call in, one reply out; "
        "backgrounds by default so you stay free meanwhile."
    )
    not_when: ClassVar[str] = (
        "firing a message without waiting, or fetching a lane's output later, which is "
        "`lane_send` plus `lane_read`."
    )

    @property
    def name(self) -> str:
        return "lane_turn"

    @property
    def input_schema(self) -> type[BaseModel]:
        return LaneTurnInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: LaneTurnInput = tool_input  # type: ignore[assignment]
        manager = resolve_lane_manager(context)
        if manager is None:
            return ToolResult(
                output="lane_turn unavailable: LaneManager not wired",
                is_error=True,
            )

        lane_id = inp.name_or_id
        named_mgr = resolve_named_lane_manager(context)
        if named_mgr is not None:
            record = await maybe_await(named_mgr.get(inp.name_or_id))
            if record is not None:
                lane_id = record.lane_id

        # Filled in by _run_foreground the moment the lane names the turn,
        # so the cancel_fn registered below can scope itself to it.
        turn_ref: dict[str, str | None] = {"turn_id": None}
        registry = getattr(context, "spawns", None)
        decision = self._resolve_dispatch(inp, registry)
        if isinstance(decision, ToolResult):
            return decision
        background, dispatch_note, flipped = decision

        if background:
            if registry is not None:
                try:
                    handle = registry.register(
                        kind=f"lane_turn:{inp.name_or_id}",
                        goal=inp.message,
                        coro=self._run_foreground(
                            inp,
                            context,
                            manager,
                            lane_id,
                            turn_ref,
                            dispatch="background",
                        ),
                        # "Background" used to background the WATCHING, not the
                        # work: cancelling the handle killed the poll loop while
                        # the lane turn ran on, and nothing called
                        # lane_interrupt. Cancel now reaches the CLI.
                        cancel_fn=_interrupt_lane(manager, lane_id, turn_ref),
                    )
                except SpawnCapExceeded as exc:
                    return spawn_cap_tool_result(exc)
                metadata: dict[str, object] = {
                    "spawn_handle": handle.handle_id,
                    "spawn_kind": f"lane_turn:{inp.name_or_id}",
                    "started_at": handle.started_at,
                    "status": "running",
                    "lane_id": lane_id,
                    "dispatch": "background",
                }
                if flipped:
                    metadata["auto_flipped"] = True
                return ToolResult(
                    output=(
                        dispatch_note
                        + f"lane_turn({inp.name_or_id}) spawned in background: "
                        f"handle={handle.handle_id}. Use spawn_check or "
                        f"spawn_await to retrieve the reply."
                    ),
                    metadata=metadata,
                )
            # Headless / REPL / MCP contexts carry no SpawnRegistry — degrade
            # to the inline path rather than failing the call, and say so:
            # the caller was told to expect a handle.

        return await self._run_foreground(
            inp, context, manager, lane_id, turn_ref, dispatch_note=dispatch_note
        )

    def _resolve_dispatch(
        self, inp: LaneTurnInput, registry
    ) -> tuple[bool, str, bool] | ToolResult:
        """Decide background-vs-inline, and say why when it is not what was asked.

        Returns `(background, note, auto_flipped)`, or a ToolResult when the
        ceiling cannot be resolved — config-as-authority, so an unreadable key
        fails the call rather than silently leaving the chat turn unbounded.

        Two ways the answer differs from `inp.background`:

        - **Asked to await, longer than the ceiling.** An inline await holds
          the whole chat turn open, queued operator messages included. A
          delegation IS a lane turn, so this honours the same bound
          `_delegate_runner` has always applied rather than inventing a second
          one.
        - **Asked to dispatch, nothing to dispatch onto.** No registry means no
          chat for a completion note to land in, so the work runs inline. The
          call still returns the reply; what changes is that it admits the
          handle never existed.
        """
        if inp.background:
            if registry is not None:
                return True, "", False
            return (
                False,
                "NOTE: this context has no spawn registry — no chat for a "
                "completion note to reach — so the turn was awaited inline "
                "and its reply is below. There is no spawn_handle to check.\n\n",
                False,
            )
        if registry is None:
            # Nothing to flip to, and the ceiling exists to protect a chat
            # turn this context does not have.
            return False, "", False
        try:
            max_foreground_s = load_max_foreground_delegate_timeout_s(
                default_runtime_config_path()
            )
        except Exception as exc:  # noqa: BLE001 — raise-loudly, surfaced to the model
            return ToolResult(
                output=f"lane_turn config error: {exc}",
                is_error=True,
            )
        _, default_timeout_s = load_conductor_relay()
        effective_s = (
            inp.timeout_s if inp.timeout_s is not None else default_timeout_s
        )
        if effective_s <= max_foreground_s:
            return False, "", False
        return (
            True,
            f"NOTE: awaiting a lane turn inline blocks this whole chat turn, "
            f"so it is capped at {max_foreground_s:.0f}s "
            f"(runtime.yaml::max_foreground_delegate_timeout_s); this call's "
            f"{effective_s:.0f}s stall ceiling exceeds it, so it was "
            f"dispatched as a background spawn instead. ",
            True,
        )

    async def _run_foreground(
        self,
        inp: LaneTurnInput,
        context: ToolContext,
        manager,
        lane_id: str,
        turn_ref: dict[str, str | None] | None = None,
        dispatch_note: str = "",
        dispatch: str = "foreground",
    ) -> ToolResult:
        poll_s, default_timeout_s = load_conductor_relay()
        timeout_s = inp.timeout_s if inp.timeout_s is not None else default_timeout_s
        reply_cap_chars = load_conductor_reply_cap()

        try:
            send_result = await maybe_await(manager.send(lane_id, inp.message))
        except Exception as exc:  # noqa: BLE001
            # M6 — a daemon restart leaves disk-alive lanes detached (named
            # ensure reuses the binding but never attaches). Self-heal once so
            # the raw lane_turn path recovers too, not just work_send.
            if "not attached" not in str(exc):
                return ToolResult(output=f"lane_turn failed: {exc}", is_error=True)
            try:
                await maybe_await(manager.attach(lane_id))
                send_result = await maybe_await(manager.send(lane_id, inp.message))
            except Exception as retry_exc:  # noqa: BLE001
                return ToolResult(
                    output=f"lane_turn failed: {retry_exc}", is_error=True
                )

        if not send_result.accepted:
            return ToolResult(
                output=f"lane_turn rejected: {send_result.reason or 'unspecified'}",
                is_error=True,
                metadata={
                    "lane_id": lane_id,
                    "accepted": False,
                    "queue_depth": send_result.queue_depth,
                },
            )
        turn_id = send_result.turn_id
        if turn_ref is not None:
            turn_ref["turn_id"] = turn_id
        if not turn_id:
            return ToolResult(
                output=(
                    "lane_turn failed: the lane accepted the message without "
                    "naming a turn, so this call cannot wait for its own reply"
                ),
                is_error=True,
                metadata={"lane_id": lane_id},
            )

        # One wait primitive, on THIS turn's id. The old loop stopped at the
        # first turn_ended it saw, so two overlapping lane_turn calls on one
        # lane each returned whichever turn finished first.
        cursor = ""
        poll_error: str | None = None
        try:
            outcome = await maybe_await(
                manager.await_turn(
                    lane_id, turn_id, timeout=timeout_s, poll_s=poll_s
                )
            )
            accumulated = list(outcome.events)
            cursor = outcome.cursor
            turn_completed = outcome.completed
            turn_is_error = outcome.is_error
        except Exception as exc:  # noqa: BLE001
            # A mid-wait failure keeps whatever the wait had already gathered
            # — a bare error here used to discard everything read so far
            # (Deferred 2026-07-12).
            accumulated = []
            turn_completed = False
            turn_is_error = False
            poll_error = str(exc)

        reply_text = "\n\n".join(
            str(ev.payload.get("text", "")).strip()
            for ev in accumulated
            if ev.kind == "assistant_text" and ev.payload.get("text")
        )
        truncated = len(reply_text) > reply_cap_chars
        if truncated:
            reply_text = reply_text[:reply_cap_chars]

        # Compact one-line-per-event summary, same shape as lane_read.py.
        lines: list[str] = []
        for ev in accumulated:
            payload_str = str(ev.payload)
            if len(payload_str) > 120:
                payload_str = payload_str[:117] + "..."
            lines.append(f"{ev.cursor}\t{ev.kind}\t{payload_str}")
        tool_activity_summary = "\n".join(lines)

        output_parts = []
        if dispatch_note:
            output_parts.append(dispatch_note.strip())
        if reply_text:
            output_parts.append(
                reply_text + (" [reply truncated]" if truncated else "")
            )
        if tool_activity_summary:
            output_parts.append(tool_activity_summary)
        if poll_error is not None:
            output_parts.append(
                f"[turn state unknown — mid-poll read failed: {poll_error}; "
                f"partial events above; continue reading with "
                f"lane_read cursor={cursor}]"
            )
        elif not turn_completed:
            output_parts.append(
                f"[turn not finished — no lane activity for {timeout_s}s; "
                f"partial events above; continue reading with "
                f"lane_read cursor={cursor}]"
            )

        metadata: dict[str, object] = {
            "reply_text": reply_text,
            "tool_activity_summary": tool_activity_summary,
            "cursor": cursor,
            "lane_id": lane_id,
            "turn_id": turn_id,
            "turn_completed": turn_completed,
            # The spawned coroutine runs this same body, so the retrieved
            # result reports the dispatch the CALLER chose, not the mechanics
            # of how the lane was then waited on.
            "dispatch": dispatch,
        }
        if dispatch_note:
            # Only set when a requested background dispatch could not be
            # honoured — an ordinary inline await carries no such claim.
            metadata["background_unavailable"] = True
        return ToolResult(
            output="\n\n".join(output_parts) if output_parts else "(no events)",
            is_error=turn_is_error or poll_error is not None,
            timed_out=not turn_completed,
            metadata=metadata,
        )
