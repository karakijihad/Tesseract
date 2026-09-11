"""playbook_record tool — how each playbook has actually done.

The numbers a verdict is given on. `playbook_judge` takes the verdict and this
is what the operator reads before giving it: how often each playbook was
reached for, how those turns closed, how many corrections followed a read, and
what those turns cost in calls and money.

**It is the panel's own reader, not a channel version of it.** The cockpit's
`/api/conscience/playbook-usage` and this tool both call
`brain/playbook_record.py::records`, so the question answers the same wherever
it is asked, which is the whole of ruling 23. A digest written for a channel
would be the second reader that ruling exists to refuse.

**It reports counts, never a verdict.** At these volumes four observations
against four move a rate 25 points, a playbook is consulted on the hard tasks
and skipped on the easy ones, and nothing on disk holds the counterfactual. So
the rule is a hand, and the numbers are what the hand reads.

Reads only: the usage log, the turn records and the cost ledger. Writes
nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.brain.playbook_record import (
    DEFAULT_WINDOW_DAYS,
    as_text,
    records,
)
from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult


class PlaybookRecordInput(BaseModel):
    days: int = Field(
        default=DEFAULT_WINDOW_DAYS,
        ge=1,
        le=365,
        description="How many days back to read. Defaults to the same window the panel opens on.",
    )
    playbook: str = Field(
        default="",
        description="One playbook by name. Empty for all of them.",
    )


class PlaybookRecordTool(Tool):
    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "finding-a-playbook"
    summary: ClassVar[str] = "How often each playbook was used, how those turns went, and what they cost."
    use_when: ClassVar[str] = (
        "Use when the operator asks how a playbook is doing, whether one is "
        "worth keeping, or which of them are being carried and never read. "
        "Read this before asking for a verdict with `playbook_judge`, so the "
        "decision is made on the numbers rather than on an impression."
    )
    not_when: ClassVar[str] = (
        "to read what a playbook says, use `playbook_search`. To act on the "
        "answer, use `playbook_judge`. This one only reports."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "none"
    recovery_behaviour: ClassVar[str] = "read_only"

    def __init__(self, skills_dir: Path) -> None:
        self._skills_dir = skills_dir

    @property
    def name(self) -> str:
        return "playbook_record"

    @property
    def input_schema(self) -> type[BaseModel]:
        return PlaybookRecordInput

    def is_read_only(self) -> bool:
        return True

    def is_concurrency_safe(self) -> bool:
        return True

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        import asyncio

        inp = (
            tool_input
            if isinstance(tool_input, PlaybookRecordInput)
            else PlaybookRecordInput(**tool_input.model_dump())
        )
        # The usage log is read whole and the turn tree is walked a day
        # directory at a time, which is well past the 50ms that blocks the
        # loop. The panel's route goes off the loop for the same call.
        rows = await asyncio.to_thread(
            records, self._skills_dir, window_days=inp.days
        )
        wanted = inp.playbook.strip()
        if wanted:
            rows = [r for r in rows if r["playbook"] == wanted]
            if not rows:
                return ToolResult(
                    output=f"No playbook called {wanted!r} that is still offered.",
                    is_error=True,
                )
        return ToolResult(output=as_text(rows, window_days=inp.days))
