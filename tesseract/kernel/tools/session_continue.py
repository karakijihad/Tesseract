"""session_continue -- what this conversation should become, decided mid-work.

A long conversation costs money on every turn whether or not the work has moved
on. Re-reading the cached prefix of a 426,000 token conversation is $0.0085 a
turn; a fresh one is $0.0006. Nothing in the runtime could tell those apart,
because the only thing that bounded a conversation was a threshold: it fires
when the window fills and knows nothing about whether the task is finished.

So the turn decides, and the decision is the same one from any surface.

**There is one consolidation and it always reflects.** What this tool records is
only what happens afterwards: keep working with the room rebuilt, or leave the
conversation behind. Whether the boundary was reached because the turn judged
it or because the window filled changes nothing about the work done at it.

**Not calling this is the third answer.** Carrying on is not a mode here. A
conversation that still fits the work needs no boundary, and a model that
believes it must pick one of two at every turn will consolidate something that
was fine.

**It takes effect at the END of the turn, not inside it.** `compact()` and
`reset()` rewrite the history in place, and doing that mid-turn folds away the
assistant message carrying the pending `tool_use` block before its
`tool_result` is appended. This records the decision;
`mirror/server/after_turn.py` acts on it, and that is the boundary both the
cockpit and a channel already call.

`default_posture="ask"`: either answer changes the conversation the operator is
in. They answer that on whatever surface they are on, and under `free` the
policy answers it for them.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.brain.chat import Continuation
from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult

log = logging.getLogger(__name__)

#: What the caller is told will happen. Written to the model, so it says the
#: consequence rather than the mechanism.
_CONFIRMED = {
    Continuation.CONTINUE: (
        "The work carries on at the end of this turn with the room made back. "
        "What this conversation taught you is being written down, and you keep "
        "the thread of what you were doing."
    ),
    Continuation.RESET: (
        "This conversation ends at the end of this turn. What it taught you is "
        "being written to memory in the background, and the next thing you are "
        "asked starts from a clean slate."
    ),
}


class SessionContinueInput(BaseModel):
    mode: Continuation = Field(
        description=(
            "continue: the work goes on, but this conversation no longer "
            "serves it, so keep the thread and get the room back. reset: the "
            "work is finished, so leave this conversation behind and keep what "
            "it taught you."
        ),
    )


class SessionContinueTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "operator_gate"

    group: ClassVar[str] = "checking-your-state"
    summary: ClassVar[str] = (
        "Carry the work into a fresh start, or finish with this conversation."
    )
    use_when: ClassVar[str] = (
        "Use when the conversation no longer fits the work. Continue when the "
        "work goes on and this conversation has stopped helping it: a phase "
        "finished, the subject changed, or most of what is behind you is "
        "settled. Reset when the work itself is finished, because carrying a "
        "conversation you are done with costs money on every turn after it."
    )
    not_when: ClassVar[str] = (
        "when the conversation still fits the work, which needs no call at "
        "all and is the ordinary case; to find out how full it is, which is "
        "`context_read`; to change when conversations fold from now on, which "
        "is `context_set` and is a lasting setting rather than a decision "
        "about this one; to save a single fact, which is `memory_save` and "
        "costs nothing."
    )
    depends_on: ClassVar[str] = ""

    @property
    def name(self) -> str:
        return "session_continue"

    @property
    def input_schema(self) -> type[BaseModel]:
        return SessionContinueInput

    def is_concurrency_safe(self) -> bool:
        return False

    def is_read_only(self) -> bool:
        return False

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        assert isinstance(tool_input, SessionContinueInput)
        if context.request_continuation is None:
            # The scheduler and autonomy build a bare `ToolContext` and run
            # turns with no conversation behind them, so there is nothing to
            # fold and nothing to leave. Saying which is the point: a caller
            # told only "failed" cannot tell whether the decision took.
            return ToolResult(
                output=(
                    "session_continue: this is not running inside a "
                    "conversation, so there is nothing to carry on or leave "
                    "behind. Nothing was changed."
                ),
                is_error=True,
            )
        mode = tool_input.mode
        try:
            context.request_continuation(mode.value)
        except Exception as exc:
            log.exception("session_continue: recording the decision failed")
            return ToolResult(
                output=(
                    f"session_continue: the decision was not recorded "
                    f"({type(exc).__name__}). This conversation carries on "
                    "unchanged, and it still folds where it did."
                ),
                is_error=True,
            )
        return ToolResult(output=_CONFIRMED[mode], metadata={"mode": mode.value})
