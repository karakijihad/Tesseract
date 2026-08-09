"""lane_send — send a follow-up message into a running lane.

X-4 Session B. ASK-gated — each message drives a CLI turn that may
execute code / call subprocesses. The contract guarantees per-lane
serial execution: `queue_depth` reports the backlog when sends arrive
while the lane is `busy`."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.config.cockpit import load_conductor_relay
from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.agent_controller.lanes.tool_support import (
    maybe_await,
    resolve_lane_manager,
)


class LaneSendInput(BaseModel):
    lane_id: str = Field(description="Lane id returned by lane_open / lane_list.")
    message: str = Field(description="Follow-up task / instruction for the lane.")
    wait: bool = Field(
        default=False,
        description=(
            "When true, block (non-busy-wait) until THIS send's turn ends, "
            "and return its reply — not just an acceptance. Timing from "
            "cockpit.yaml conductor.relay_*. Falls back to plain send if the "
            "manager has no send_and_await."
        ),
    )


class LaneSendTool(Tool):
    default_posture = "ask"
    risk_class: ClassVar[str] = "operator_gate"

    # A CLI's reply is whatever it read out of a repository, a web page, or
    # a compromised model — untrusted by origin, however trusted the tool
    # that fetched it. Set here so the foreground result and `spawn_await`'s
    # retrieval get the same envelope the background completion delivery
    # applies in `chat.py::_format_spawn_completion`; wrapping only the one
    # path left the other two open.
    untrusted_source: ClassVar[bool] = True


    @property
    def name(self) -> str:
        return "lane_send"

    @property
    def description(self) -> str:
        return (
            "Send a follow-up message into a running lane. Drives one "
            "CLI turn; events stream into the lane's events.jsonl. "
            "Strict per-lane FIFO. Returns the accepted turn_id; with "
            "wait=true, returns that turn's reply and whether it completed."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return LaneSendInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: LaneSendInput = tool_input  # type: ignore[assignment]
        manager = resolve_lane_manager(context)
        if manager is None:
            return ToolResult(
                output="lane_send unavailable: LaneManager not wired",
                is_error=True,
            )
        async def _dispatch():
            send_and_await = getattr(manager, "send_and_await", None)
            if inp.wait and send_and_await is not None:
                poll_s, timeout_s = load_conductor_relay()
                return await send_and_await(
                    inp.lane_id, inp.message, timeout=timeout_s, poll_s=poll_s
                )
            return await manager.send(inp.lane_id, inp.message)

        try:
            result = await _dispatch()
        except Exception as exc:  # noqa: BLE001
            # M6 self-heal, same as lane_turn: a daemon restart leaves
            # disk-alive lanes detached — attach once and retry.
            if "not attached" not in str(exc):
                return ToolResult(
                    output=f"lane_send failed: {exc}",
                    is_error=True,
                )
            try:
                await maybe_await(manager.attach(inp.lane_id))
                result = await _dispatch()
            except Exception as retry_exc:  # noqa: BLE001
                return ToolResult(
                    output=f"lane_send failed: {retry_exc}",
                    is_error=True,
                )
        if not result.accepted:
            return ToolResult(
                output=f"lane_send rejected: {result.reason or 'unspecified'}",
                is_error=True,
                metadata={
                    "lane_id": inp.lane_id,
                    "accepted": False,
                    "queue_depth": result.queue_depth,
                },
            )
        metadata = {
            "lane_id": inp.lane_id,
            "accepted": True,
            "queue_depth": result.queue_depth,
            "turn_id": result.turn_id,
        }
        outcome = result.outcome
        if not inp.wait or outcome is None:
            return ToolResult(
                output=(
                    f"accepted lane_id={inp.lane_id} turn_id={result.turn_id} "
                    f"queue_depth={result.queue_depth}"
                ),
                metadata=metadata,
            )

        # `wait=True` asked a question; "accepted" is not an answer to it.
        # The old return said `accepted lane_id=… queue_depth=…` whether the
        # wait saw a completion or silently hit the stall deadline, so the
        # model could not tell the difference and never got the reply.
        metadata.update(
            {
                "turn_completed": outcome.completed,
                "cursor": outcome.cursor,
                "reply_text": outcome.reply_text,
            }
        )
        if not outcome.completed:
            return ToolResult(
                output=(
                    f"{outcome.reply_text}\n\n[turn not finished — no lane "
                    f"activity within the relay timeout; partial reply above, "
                    f"continue with lane_read cursor={outcome.cursor}]"
                ).strip(),
                is_error=False,
                timed_out=True,
                metadata=metadata,
            )
        if outcome.is_error:
            return ToolResult(
                output=(
                    f"{outcome.reply_text}\n\n[lane turn failed: "
                    f"{outcome.error or 'the CLI reported a failed turn'}]"
                ).strip(),
                is_error=True,
                metadata=metadata,
            )
        return ToolResult(
            output=outcome.reply_text or "(the turn completed with no reply)",
            metadata=metadata,
        )
