"""context_read — how full this conversation is, to whoever asked.

The cockpit HUD draws a bar for tokens and turns, and it was the only thing
that could. `emit_stats` measured the numbers straight into a WebSocket
envelope, so a channel had no way to ask and neither did the assistant: "how
much room is left before you forget the start of this?" failed on a phone AND
in the cockpit's own chat, which is the same fork `autonomy_read` was built to
close.

**A reader, not a channel feature.** It assembles nothing. `brain/
context_report.py` is the one measurement, the HUD's envelope is built from it
too, and there is no second idea of where the fold happens. Whatever the bar
would draw, this says, in words.

`default_posture="auto"` — it reads the shape of the conversation it is already
running inside, and reports no message content.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from pydantic import BaseModel

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult

log = logging.getLogger(__name__)


class ContextReadInput(BaseModel):
    """No arguments. The conversation being asked about is the one asking."""


class ContextReadTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "checking-your-state"
    summary: ClassVar[str] = (
        "How full this conversation is: tokens, turns, cache, and its next fold."
    )
    use_when: ClassVar[str] = (
        "Use when you are asked how much room is left, how long this "
        "conversation has run, whether you are about to forget its earlier "
        "part, what the window is, or how much of the prompt came from "
        "cache. Relay the block as it is: it is the picture the cockpit "
        "draws, and a sentence of your own throws that away."
    )
    not_when: ClassVar[str] = (
        "for what a past conversation contained, which is `recall_history`; "
        "for what the runtime as a whole is doing, which is `autonomy_read`; "
        "for this machine's hardware and provider health, which is "
        "`system_diagnose`. It says nothing about money either: spend and "
        "caps are the cost ledger's, not this."
    )
    depends_on: ClassVar[str] = ""

    @property
    def name(self) -> str:
        return "context_read"

    @property
    def input_schema(self) -> type[BaseModel]:
        return ContextReadInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        del tool_input
        if context.context_report is None:
            # The scheduler and autonomy build a bare `ToolContext` and run
            # turns nobody is sitting in. A sub-agent is NOT one of these: it
            # gets a real `ChatSession` and its own wiring, so it measures the
            # sub-conversation it is having, which is the right answer to give
            # it. Say which question went unanswered rather than reporting
            # zeros, which would read as an empty conversation with all its
            # room left.
            return ToolResult(
                output=(
                    "context_read: this turn is not running inside a "
                    "conversation, so there is no context to measure. Nothing "
                    "is known about how full it is, which is not the same as "
                    "it being empty."
                ),
                is_error=True,
            )

        from tesseract.brain import context_report

        try:
            report = context.context_report()
        except Exception as exc:
            # The token count is the report's spine and `gather` lets it raise.
            # Naming the failure beats a bar drawn from a zero. The type only:
            # this output is sent to whichever chat asked, a channel included,
            # and an exception's own message can carry a path off this machine.
            log.exception("context_read: measuring the conversation failed")
            return ToolResult(
                output=(
                    "context_read: this conversation could not be measured "
                    f"({type(exc).__name__}). How full it is is unknown, so "
                    "do not treat it as having room."
                ),
                is_error=True,
            )

        return ToolResult(
            output=context_report.render(report),
            metadata=report,
        )
