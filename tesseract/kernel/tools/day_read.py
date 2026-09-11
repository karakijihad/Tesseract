"""day_read — what the runtime actually did on a day, to whoever asked.

The records have been on disk for weeks and nothing could ask them. Turn
manifests are read by the atlas builder, by boot recovery and by playbook
extraction, and by no surface at all, so "what did you do yesterday" had one
honest answer: open a directory of JSON.

This is `autonomy_read`'s argument applied to a second question, and ruling 23
is the same ruling. What the operator can ask for at the desk they can ask for
from a channel, and a panel is not an answer to somebody holding a phone. So
the panel's route and this tool call ONE reader, `orchestrator/turns/day.py`.
There is no channel shape here, no digest of its own, and nothing that decides
what a day contains for a second time. Whatever the panel would draw, this
says, from the same pass over the same records.

`default_posture="auto"` and read only: it reads local records the operator
owns, and opening the panel that draws them costs no approval either.

**It reports and never repairs.** A reader with an opinion about the record is
a reader that has stopped being evidence, and this record is what a verdict
will later be computed against.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import ClassVar, Optional

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.outcome import RunOutcome

#: What one reply may list before it stops being readable. A day of heavy use
#: is hundreds of calls, and a channel message carrying all of them is not an
#: answer, it is a file. The roster is always complete; the individual calls
#: are what gets capped, and the reply says so rather than trailing off.
_TROUBLE_CAP = 25
_TOOL_CAP = 40

#: What a reason may cost on one line. A recorded reason is whatever the tool
#: said, and `bash` says a whole test log: one real row on this machine carried
#: 400 characters of captured stderr, which pushed the twelve rows after it off
#: a phone screen. The record keeps all of it; a reply quotes the start.
_REASON_CHARS = 130

#: Plain words for the outcome, because the enum's are for the code. Read from
#: this rather than from `liveness.LABELS`: that vocabulary answers "is this
#: capability well", where `caller_error` is correctly quiet, and a row saying
#: a failed call was "quiet" is worse than the slug.
_WORDS = {
    RunOutcome.FAILED: "it failed",
    RunOutcome.CALLER_ERROR: "it was asked for something it could not do",
    RunOutcome.UNVERIFIED: "it worked and left nothing to check",
    RunOutcome.REFUSED: "it was not allowed to start",
    RunOutcome.TRUNCATED: "it ran out of time",
    RunOutcome.DEGRADED: "it did less than it promises",
    RunOutcome.SKIPPED_NO_WORK: "there was nothing to do",
    RunOutcome.SKIPPED_UPSTREAM_FAILED: "what it needed had not succeeded",
}


def _one_line(reason: str) -> str:
    """A recorded reason, trimmed to something a person reads in one glance."""
    flat = " ".join(reason.split())
    return flat if len(flat) <= _REASON_CHARS else flat[: _REASON_CHARS - 1] + "…"


class DayReadInput(BaseModel):
    on: Optional[str] = Field(
        default=None,
        description=(
            "The day to read, as YYYY-MM-DD. Omit for the most recent day "
            "that has records, which is usually today."
        ),
    )
    through: Optional[str] = Field(
        default=None,
        description=(
            "Read a span instead of one day, ending here. YYYY-MM-DD, at most "
            "31 days from `on`."
        ),
    )
    only_trouble: bool = Field(
        default=False,
        description=(
            "Report only the calls that were not clean successes, and skip the "
            "per-tool roster. Use when the question is whether anything went "
            "wrong rather than what was used."
        ),
    )


class DayReadTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "checking-your-state"
    summary: ClassVar[str] = "Read what the runtime actually did on a day: tools, outcomes, marks."
    use_when: ClassVar[str] = (
        "Use when the question is what happened over a stretch of time rather "
        "than what is happening now: what you used yesterday, whether anything "
        "failed overnight, how a day went, what a tool was called for. Reads "
        "the turn records the runtime writes for itself, so it answers the "
        "same way here as on the panel."
    )
    not_when: ClassVar[str] = (
        "for what is happening right now, which is `autonomy_read`. For what "
        "was SAID in a past session use `recall_history`: this reads what the "
        "runtime did, never the conversation."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "none"
    recovery_behaviour: ClassVar[str] = "read_only"

    @property
    def name(self) -> str:
        return "day_read"

    @property
    def input_schema(self) -> type[BaseModel]:
        return DayReadInput

    def is_read_only(self) -> bool:
        return True

    def is_concurrency_safe(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        from tesseract.orchestrator.turns import day as day_reader

        inp = (
            tool_input
            if isinstance(tool_input, DayReadInput)
            else DayReadInput(**tool_input.model_dump())
        )

        try:
            on = date.fromisoformat(inp.on) if inp.on else None
            through = date.fromisoformat(inp.through) if inp.through else None
        except ValueError:
            # The caller is the model and this is the caller getting it wrong,
            # which is a different thing from the reader being unwell.
            return ToolResult(
                output=(
                    "day_read: dates are YYYY-MM-DD. Got "
                    f"on={inp.on!r}, through={inp.through!r}."
                ),
                is_error=True,
                caller_error=True,
            )

        anchor = on or await asyncio.to_thread(day_reader.latest_day_with_records)
        if anchor is None:
            return ToolResult(
                output=(
                    "There are no records of any day yet. Nothing has run since "
                    "the records were last cleared, or this is a fresh install."
                ),
            )

        report = await asyncio.to_thread(day_reader.day, anchor, through)
        return ToolResult(output=_render(report, only_trouble=inp.only_trouble))


def _render(report, *, only_trouble: bool) -> str:
    """The day in sentences, in the vocabulary the panel uses.

    Plain text and never Markdown: a tool name carries underscores, and one of
    those is enough to lose a whole message on a channel that parses them.
    """
    from tesseract.orchestrator.turns.manifest import door_name

    if not report.days:
        return "There are no records of any day yet."

    when = (
        report.days[0].isoformat()
        if len(report.days) == 1
        else f"{report.days[0].isoformat()} to {report.days[-1].isoformat()}"
    )
    doors = ", ".join(
        f"{n} from {door_name(entry)}"
        for entry, n in sorted(report.by_door.items(), key=lambda kv: -kv[1])
    )
    lines = [
        f"{when}: {len(report.turns)} turns, {len(report.calls)} tool calls."
        + (f" {doors[:1].upper()}{doors[1:]}." if doors else "")
    ]
    if report.unreadable:
        lines.append(
            f"{report.unreadable} turn records could not be read, so they are "
            "in none of the counts below."
        )

    trouble = report.trouble
    if not trouble:
        lines.append("Every call came back clean.")
    else:
        lines.append("")
        lines.append(f"Not clean ({len(trouble)}):")
        for call in trouble[:_TROUBLE_CAP]:
            lines.append(
                f"  {call.at.strftime('%H:%M')} {call.tool}: "
                f"{_WORDS.get(call.outcome, call.outcome.value)}"
                + (f". {_one_line(call.reason)}" if call.reason else ".")
            )
        if len(trouble) > _TROUBLE_CAP:
            lines.append(f"  and {len(trouble) - _TROUBLE_CAP} more.")

    if not only_trouble and report.tools:
        lines.append("")
        lines.append(f"What it used ({len(report.tools)} tools):")
        for row in report.tools[:_TOOL_CAP]:
            counts = ", ".join(
                f"{n} {word}" for word, n in sorted(row.by_outcome.items())
            )
            marks = f", {row.receipts} left a mark" if row.receipts else ""
            plural = "call" if row.calls == 1 else "calls"
            lines.append(f"  {row.tool}: {row.calls} {plural} ({counts}){marks}")
        if len(report.tools) > _TOOL_CAP:
            lines.append(f"  and {len(report.tools) - _TOOL_CAP} more.")

    return "\n".join(lines)


__all__ = ["DayReadInput", "DayReadTool"]
