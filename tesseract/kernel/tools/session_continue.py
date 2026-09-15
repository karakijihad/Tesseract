"""session_continue -- what this conversation should become, decided mid-work.

A long conversation costs money on every turn whether or not the work has moved
on. Re-reading the cached prefix of a 426,000 token conversation is $0.0085 a
turn; a fresh one is $0.0006. Nothing in the runtime could tell those apart,
because the only thing that bounded a conversation was a threshold: it fires
when the window fills and knows nothing about whether the task is finished.

So the turn decides, and the decision is the same one from any surface.

**There is one consolidation and it always reflects.** What this tool decides is
only what happens afterwards: keep working with the room rebuilt, or leave the
conversation behind. Whether the boundary was reached because the turn judged
it or because the window filled changes nothing about the work done at it.

**Not calling this is the third answer.** Carrying on is not a mode here. A
conversation that still fits the work needs no boundary, and a model that
believes it must pick one of two at every turn will consolidate something that
was fine.

**What the three answers mean is stated in `OPERATING.md`, once.** It used to
be here as well, in `use_when` and on the `mode` field, which put the criteria
in front of the model twice in slightly different words and left carrying on
described only by the tool that does not do it. The document is in the head of
every prompt on every surface, so the model reads it whether or not it is
looking at this schema. What belongs here is when to reach for THIS tool rather
than a neighbouring one, and nothing about how to judge the work.

**One call carries both halves: the answer and the handoff.** Where the work
stands is written by the agent that did it, in the same call, because they are
one answer. The reflection used to reconstruct it afterwards from the
transcript, which cost a model turn over the whole conversation, delayed the
handover by as long as that turn took, and made a failed reflection cost the
work rather than the learning. Asking the agent, which held the list all
along, costs nothing.

A handoff with nothing remaining and no next action is a real answer and not
an omission: it says the work is finished, and the runtime turns the boundary
into a `reset` that says so rather than asking again. The cheapest way out of
being asked again is to invent a remaining item, so it never asks.

**It takes effect at the end of the turn, and the turn ends right here.**
`reset()` rewrites the history in place, and doing that inside this call would
fold away the assistant message carrying the pending `tool_use` block before
its `tool_result` is appended, so this only records the decision. What is
different now is how soon "the end of the turn" arrives: once this step's tool
results (this call's and any it ran alongside) are back, the runtime stops
asking the model for another step and the turn ends there, so the boundary
this recorded is taken next, not after however much more work the turn would
otherwise have gone on to do. `mirror/server/after_turn.py` is that boundary,
and it is the one both the cockpit and a channel already call.

A turn that outgrows the ceiling on its own is answered the same way and for
the same reason: it is let finish, and the boundary it owes is taken the
moment it does.

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
        "This turn ends now, once this step's tool results are back. The work "
        "carries on after that with the room made back. What you wrote about "
        "where the work stands is handed to the turn that follows, and what "
        "this conversation taught you is being written to memory alongside it."
    ),
    Continuation.RESET: (
        "This turn ends now, once this step's tool results are back, and this "
        "conversation ends with it. What you wrote about where the work "
        "stands is kept and what it taught you is being written to memory in "
        "the background, and the next thing you are asked starts from a "
        "clean slate."
    ),
}


class SessionContinueInput(BaseModel):
    """The answer, and the handoff that goes with it.

    Every field but `mode` is optional and empty is a legitimate value. A
    conversation that was never about a piece of work leaves them all empty,
    and that is recorded rather than guessed at: the next context acts on what
    is written here, so an invented next action is worse than none.
    """

    mode: Continuation = Field(
        description=(
            "Which of the two answers this is. What each one means for the "
            "work, and the third answer that needs no call, are in "
            "OPERATING.md."
        ),
    )
    objective: str = Field(
        default="",
        description="What this conversation was trying to achieve, in a line.",
    )
    phase: str = Field(
        default="",
        description="Where in that objective the work had got to.",
    )
    completed: list[str] = Field(
        default_factory=list,
        description="What actually got done here. One item per thing.",
    )
    remaining: list[str] = Field(
        default_factory=list,
        description=(
            "Work somebody will do next, and nothing else. Not what the "
            "project decided to leave out, not stages waiting on approval, "
            "not standing scope notes. Those are true and they are not "
            "remaining work. Leaving this empty alongside an empty "
            "`next_action` says the work is finished, and the runtime leaves "
            "the conversation behind rather than carrying it on."
        ),
    )
    next_action: str = Field(
        default="",
        description="The single step to take first, if the work carries on.",
    )
    open_questions: list[str] = Field(
        default_factory=list,
        description="What is undecided and who has to decide it.",
    )
    blocked_by: str = Field(
        default="",
        description="What is in the way, if anything is.",
    )
    artifacts: list[str] = Field(
        default_factory=list,
        description=(
            "Paths and identifiers, never file contents. You do not need to "
            "say where the project lives: the runtime records that beside "
            "them."
        ),
    )


class SessionContinueTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "operator_gate"

    group: ClassVar[str] = "checking-your-state"
    summary: ClassVar[str] = (
        "Say where the work stands, then carry it into a fresh start or leave "
        "it behind."
    )
    use_when: ClassVar[str] = (
        "Use when you have decided this conversation should end, on the "
        "grounds OPERATING.md gives for each answer. Say where the work "
        "stands in the same call: what it was for, what got done, what is "
        "left and what to do first. That is the only record the next context "
        "gets, and nothing else writes it. The runtime carries the decision "
        "out once the turn is over, so finish what you are saying first."
    )
    not_when: ClassVar[str] = (
        "for a conversation you have not decided to end, which needs no call "
        "at all; to find out how full it is, which is `context_read`; to "
        "change when conversations fold from now on, which is `context_set` "
        "and is a lasting setting rather than a decision about this one; to "
        "save a single fact, which is `memory_save` and costs nothing."
    )
    depends_on: ClassVar[str] = ""
    # It records a decision about THIS conversation, which the turn's own
    # record already carries. Nothing separate to point at.
    receipt_kind: ClassVar[str] = "none"
    recovery_behaviour: ClassVar[str] = "idempotent"

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
        state = tool_input.model_dump(exclude={"mode"})
        try:
            context.request_continuation(mode.value, state)
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
