"""lane_turn — send a message, await the turn's end, return the reply.

Composite of lane_send(wait=True) + lane_read: one call replaces the
send -> poll lane_read -> parse dance. `name_or_id` resolves through
the named-lane binding layer first (falls back to treating it as a raw
lane_id when no binding exists), then drives a generic send-and-poll
loop via `maybe_await` so it works on both the real in-process
LaneManager (sync read, async send) and the Mirror IPC proxy (fully
async). Semantics mirror `IpcLaneManager.send_and_await`: the tail
cursor is captured BEFORE send so a lane's history can never be
mistaken for the current turn's events."""

from __future__ import annotations

import asyncio
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.config.cockpit import load_conductor_relay, load_conductor_reply_cap
from tesseract.kernel.tools.base import (
    SpawnCapExceeded,
    Tool,
    ToolContext,
    ToolResult,
    spawn_cap_tool_result,
)
from tesseract.orchestrator.tars_controller.lanes.tool_support import (
    maybe_await,
    resolve_lane_manager,
    resolve_named_lane_manager,
)


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
            "Fire-and-track (default): send the message, return a "
            "spawn_handle immediately; retrieve the lane's reply via "
            "spawn_check / spawn_await, or wait for the completion note. "
            "Pass false only when the very next step in THIS turn must "
            "consume the lane's reply inline."
        ),
    )


class LaneTurnTool(Tool):
    default_posture = "ask"
    risk_class: ClassVar[str] = "operator_gate"

    @property
    def name(self) -> str:
        return "lane_turn"

    @property
    def description(self) -> str:
        return (
            "Send a message into a named or raw lane and track the turn — "
            "the default verb for Claude/Codex lane collaboration. "
            "Backgrounds by default: returns a spawn_handle immediately; "
            "the reply arrives via spawn_check / spawn_await or the "
            "completion note. The wait follows the lane's event stream "
            "until turn_ended; timeout_s bounds silence (a stalled lane), "
            "not total turn duration — long active turns are normal. On "
            "stall, returns partial events + cursor (turn_completed=False) "
            "so the caller can lane_read later. is_error reflects the lane "
            "turn's own outcome when it completes (turn_ended.is_error), "
            "not just tool-plumbing failures."
        )

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

        if inp.background:
            registry = getattr(context, "spawns", None)
            if registry is not None:
                try:
                    handle = registry.register(
                        kind=f"lane_turn:{inp.name_or_id}",
                        goal=inp.message,
                        coro=self._run_foreground(inp, context, manager, lane_id),
                    )
                except SpawnCapExceeded as exc:
                    return spawn_cap_tool_result(exc)
                return ToolResult(
                    output=(
                        f"lane_turn({inp.name_or_id}) spawned in background: "
                        f"handle={handle.handle_id}. Use spawn_check or "
                        f"spawn_await to retrieve the reply."
                    ),
                    metadata={
                        "spawn_handle": handle.handle_id,
                        "spawn_kind": f"lane_turn:{inp.name_or_id}",
                        "started_at": handle.started_at,
                        "status": "running",
                        "lane_id": lane_id,
                    },
                )
            # Headless / REPL contexts carry no SpawnRegistry — degrade to
            # the inline path rather than failing the call.

        return await self._run_foreground(inp, context, manager, lane_id)

    async def _run_foreground(
        self,
        inp: LaneTurnInput,
        context: ToolContext,
        manager,
        lane_id: str,
    ) -> ToolResult:
        poll_s, default_timeout_s = load_conductor_relay()
        timeout_s = inp.timeout_s if inp.timeout_s is not None else default_timeout_s
        reply_cap_chars = load_conductor_reply_cap()

        try:
            _, cursor = await maybe_await(manager.read(lane_id, None))
            send_result = await maybe_await(manager.send(lane_id, inp.message))
        except Exception as exc:  # noqa: BLE001
            # M6 — a daemon restart leaves disk-alive lanes detached (named
            # ensure reuses the binding but never attaches). Self-heal once so
            # the default trio path (raw lane_turn) recovers, not just work_send.
            if "not attached" not in str(exc):
                return ToolResult(output=f"lane_turn failed: {exc}", is_error=True)
            try:
                await maybe_await(manager.attach(lane_id))
                _, cursor = await maybe_await(manager.read(lane_id, None))
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

        accumulated = []
        turn_completed = False
        turn_is_error = False
        poll_error: str | None = None
        try:
            loop = asyncio.get_running_loop()
            # Stall-based deadline: lane activity extends its own wait —
            # timeout_s bounds SILENCE, not total turn duration (2026-07-13
            # incident: a wall-clock cap abandoned healthy long turns).
            deadline = loop.time() + timeout_s
            while loop.time() < deadline:
                events, cursor = await maybe_await(manager.read(lane_id, cursor))
                accumulated.extend(events)
                if events:
                    deadline = loop.time() + timeout_s
                turn_ended = next((ev for ev in events if ev.kind == "turn_ended"), None)
                if turn_ended is not None:
                    turn_completed = True
                    turn_is_error = bool(turn_ended.payload.get("is_error"))
                    break
                await asyncio.sleep(poll_s)
        except Exception as exc:  # noqa: BLE001
            # Preserve accumulated events + cursor on a mid-poll failure the
            # same way the timeout path does — a bare error here used to
            # discard everything already read (Deferred 2026-07-12).
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

        return ToolResult(
            output="\n\n".join(output_parts) if output_parts else "(no events)",
            is_error=turn_is_error or poll_error is not None,
            timed_out=not turn_completed,
            metadata={
                "reply_text": reply_text,
                "tool_activity_summary": tool_activity_summary,
                "cursor": cursor,
                "lane_id": lane_id,
                "turn_completed": turn_completed,
            },
        )
