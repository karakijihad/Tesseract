"""retention_set_window — decide how long this machine keeps one of its logs.

The room that shows what gets thrown away could only be read. Every window in
it is the operator's number, and the only way to change one was to open
`config/retention.yaml` in an editor on the machine, which is a control that
exists at the desk and nowhere else. Away from it for three days, a log growing
by the hour is something you can watch and not something you can do anything
about.

So it is a tool rather than a field the room writes through a route of its own.
The room's control, the same words typed in the cockpit, and the same words
said on a phone all end up here, and there is one place that decides what a
window may be.

**It changes the window and nothing else.** What a sweep DOES to a file, and
whether a tree may be deleted from at all, stay in the code for the reason
`may_delete` exists: a permission ledger that can be pruned by editing a
number is a ledger with no policy. Every refusal comes back in the words the
next boot would use, because a control that accepts what the file refuses
leaves a machine that will not start.

`default_posture="ask"`: this decides how long the record of what the machine
did survives, and the shortest window it accepts still deletes things. The
operator pressing the room's field, or typing the command, runs through
`run_slash`, which skips posture because they already are the approval. The
model asking to shorten a window is asked.
"""

from __future__ import annotations

import asyncio
import logging
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.kernel.tools.receipt import Receipt

logger = logging.getLogger(__name__)


class RetentionSetWindowInput(BaseModel):
    tree: str = Field(
        description=(
            "Which log to change, by the name it is shown under (for example "
            "'Every background job it has run') or by its key (for example "
            "'scheduler_runs'). Naming one that does not exist answers with "
            "the whole list."
        ),
    )
    days: int = Field(
        description=(
            "How many days to keep it for. Whole days, one at the least. "
            "What happens at the end of the window is not set here."
        ),
    )


class RetentionSetWindowTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "propose"

    group: ClassVar[str] = "checking-your-state"
    summary: ClassVar[str] = (
        "Sets how many days one of this machine's own logs is kept for."
    )
    use_when: ClassVar[str] = (
        "Use when the operator wants something kept longer or cleared out "
        "sooner: a log they want a longer history of, or one that is growing "
        "and they want held down. It answers with what the window was, what "
        "it now is, and what happens at the end of it, so relay that rather "
        "than saying it was changed."
    )
    not_when: ClassVar[str] = (
        "to see what is kept and how big it has grown, which is "
        "`autonomy_read`; to run the nightly sweep now, which is "
        "`pipeline_run_stage`; to change what a sweep does to a file, or to "
        "give a window to something that has none, both of which are the "
        "code's and not a setting."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "record"
    recovery_behaviour: ClassVar[str] = "idempotent"

    @property
    def name(self) -> str:
        return "retention_set_window"

    @property
    def input_schema(self) -> type[BaseModel]:
        return RetentionSetWindowInput

    def is_concurrency_safe(self) -> bool:
        # It writes a file. This only keeps two calls in ONE turn apart,
        # though; two turns, or the room's field and a phone at once, are held
        # by `set_window`'s own lock, which is where the round trip is.
        return False

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        del context
        from tesseract import paths
        from tesseract.orchestrator.panel_refresh import RETENTION_KEY, publish_stale
        from tesseract.retention.policy import RetentionError, set_window

        inp: RetentionSetWindowInput = tool_input  # type: ignore[assignment]
        try:
            was, now = await asyncio.to_thread(
                set_window, paths.config_dir(), inp.tree.strip(), inp.days
            )
        except RetentionError as exc:
            return ToolResult(output=str(exc), is_error=True, caller_error=True)
        except OSError as exc:
            logger.exception("retention_set_window: the table could not be written")
            return ToolResult(
                output=(
                    "The retention table could not be written, so nothing "
                    f"changed and {inp.tree.strip()} is still on the window it "
                    f"had ({type(exc).__name__}). The backend log names the "
                    "file."
                ),
                is_error=True,
            )

        # The room shows a window it read, and it polls slowly because a file
        # tree does not move. A window DOES, and the operator who just changed
        # it is looking at the row. Best effort, and after the write: a change
        # that landed must not be undone because no panel was listening.
        if was.keep_days != now.keep_days:
            publish_stale(RETENTION_KEY)

        if was.keep_days == now.keep_days:
            said = (
                f"{now.tree.title} was already on that window, so nothing "
                f"changed. {now.in_words()}"
            )
        else:
            said = (
                f"{now.tree.title} was kept {was.keep_days} days and is now "
                f"kept {now.keep_days}. {now.in_words()} The next nightly "
                "sweep uses it."
            )
        return ToolResult(
            output=said,
            receipt=(
                Receipt(
                    kind="record",
                    id=now.tree.key,
                    locator=str(paths.config_dir() / "retention.yaml"),
                )
                if was.keep_days != now.keep_days
                else Receipt.nothing()
            ),
            metadata={
                "tree": now.tree.key,
                "was": was.keep_days,
                "days": now.keep_days,
                "action": now.action.value,
            },
        )


__all__ = ["RetentionSetWindowInput", "RetentionSetWindowTool"]
