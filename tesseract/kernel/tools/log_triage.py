"""LogTriageTool — summarise a log instead of grepping it blind.

The instinct that produced this: faced with a 1 MB backend log, the right move
is to ask what is wrong with it, and the only available tool was a regex
search that returned 200 truncated lines. Whether that sample was
representative was unknowable, so the conclusion drawn from it was a guess
wearing evidence's clothes.

So this reads the whole file and reports shape: how many records, over what
span, at which levels, from which loggers, and which distinct messages repeat.
Distinctness is computed on a normalised form — timestamps, durations, ids and
paths replaced with placeholders — because ninety occurrences of one failing
request are one problem, and a raw tail shows them as ninety.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools._path_anchor import ReadPathRefused, anchor_read_path
from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult

# Two formatters ship, and the second one is the supervisor's:
#   logsetup:    `2026-08-10 15:58:43,204 INFO tesseract.logsetup: message`
#   supervisor:  `2026-08-09 16:43:32,694 ERROR tesseract.supervisor.reap message`
#
# `supervisor/__main__.py:57-59` uses `%(name)s %(message)s` with no colon, so
# a pattern demanding one reported "no parseable log records" for the whole of
# `runtime/logs/supervisor.log` — a tool for reading the log trees, blind to
# one of them. The logger name is a dotted token with no spaces, which is what
# separates it from the message in the colonless form.
_RECORD = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3}\s+"
    r"(?P<level>[A-Z]+)\s+(?P<logger>[\w.\-]+)(?::\s*|\s+)(?P<message>.*)$"
)

_LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "WARN": 30, "ERROR": 40, "CRITICAL": 50}

# Applied in order. The goal is that two records describing the same event
# collapse to one key, without collapsing records that differ in kind.
_NORMALISERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[A-Za-z]:\\[^\s'\"]+"), "<path>"),
    (re.compile(r"(?<![\w.])/(?:[\w.\-]+/)+[\w.\-]+"), "<path>"),
    # At least one non-digit hex character, so a long run of plain digits falls
    # through to the numeric rules below. Without that, "took 12345678 ms"
    # normalised to `<id>` while "took 1234567 ms" normalised to `<n>`, and two
    # records of one event landed in different groups because a number crossed
    # eight digits.
    (re.compile(r"\b(?=[0-9a-fA-F]{8,}\b)[0-9a-fA-F]*[a-fA-F][0-9a-fA-F]*\b"), "<id>"),
    # No trailing \b: a number is usually followed by its unit, and digit->letter
    # is not a word boundary, so `\b\d+\.\d+\b` fails to match the `2.203` in
    # "lag 2.203s" — which left every distinct duration as its own group and
    # defeated the whole point of grouping.
    (re.compile(r"(?<!\w)\d+\.\d+"), "<n>"),
    (re.compile(r"(?<!\w)\d+"), "<n>"),
)

_MAX_BYTES = 32 * 1024 * 1024

# Ceiling on the continuation text glued onto one record. A traceback is a few
# hundred bytes; this is generous for that and still bounds the pathological
# case, where a file that is not a log opens with one log-shaped line and every
# subsequent line is appended to it. That produced a single multi-megabyte
# string, which `normalise` then ran five regex substitutions across.
_MAX_CONTINUATION_CHARS = 4096


def normalise(message: str) -> str:
    """Collapse the varying parts of a message so repeats group together."""
    for pattern, placeholder in _NORMALISERS:
        message = pattern.sub(placeholder, message)
    return message.strip()


@dataclass
class _Record:
    stamp: datetime | None
    level: str
    logger: str
    message: str
    truncated: bool = False


class LogTriageInput(BaseModel):
    path: str = Field(
        description=(
            "Log file to summarise. Relative paths anchor at the code tree — "
            "the log trees (home/logs/**, runtime/logs/**) need absolute paths."
        )
    )
    min_level: str = Field(
        default="WARNING",
        description="Lowest level to include in the message breakdown (DEBUG/INFO/WARNING/ERROR/CRITICAL).",
    )
    since_hours: float | None = Field(
        default=None,
        gt=0,
        description="Only consider records newer than this many hours. Omit for the whole file.",
    )
    top: int = Field(default=15, gt=0, le=100, description="How many distinct messages to list.")


class LogTriageTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"
    # This tool echoes text straight out of a file, exactly as `file_read`
    # does, so it carries `file_read`'s protection too. A log is written by
    # every subsystem and by anything they quote — an HTTP body, a filename, a
    # transcript — so a line arriving here is not the runtime's own voice and
    # must not enter chat history as though it were.
    untrusted_source: ClassVar[bool] = True

    group: ClassVar[str] = "files-on-disk"
    summary: ClassVar[str] = "Summarise a log file's shape: counts, span, levels, and repeated messages."
    use_when: ClassVar[str] = (
        "The question is 'what is wrong in this log' rather than 'does this "
        "exact string appear' — reads the whole file and groups repeats so one "
        "failing request does not read as many distinct problems."
    )
    not_when: ClassVar[str] = "Use `grep` when you already know the exact string to find."

    @property
    def name(self) -> str:
        return "log_triage"

    @property
    def input_schema(self) -> type[BaseModel]:
        return LogTriageInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        if context.cancel_event.is_set():
            raise asyncio.CancelledError
        inp = (
            tool_input
            if isinstance(tool_input, LogTriageInput)
            else LogTriageInput(**tool_input.model_dump())
        )

        try:
            path = anchor_read_path(inp.path, context.workspace_root)
        except ReadPathRefused as exc:
            return ToolResult(output=str(exc), is_error=True)
        if not path.exists():
            return ToolResult(output=f"Log not found: {path}", is_error=True)
        if path.is_dir():
            return ToolResult(
                output=(
                    f"{path} is a directory. Name one log file — use `glob` to "
                    "list what is in the tree first."
                ),
                is_error=True,
            )

        size = path.stat().st_size
        if size > _MAX_BYTES:
            return ToolResult(
                output=(
                    f"{path} is {size / 1024**2:.1f} MB, over the {_MAX_BYTES / 1024**2:.0f} MB "
                    "triage limit. Narrow it with `since_hours`, or split the file."
                ),
                is_error=True,
            )

        floor = _LEVEL_ORDER.get(inp.min_level.upper())
        if floor is None:
            return ToolResult(
                output=f"Unknown level {inp.min_level!r}. Use one of: {', '.join(sorted(set(_LEVEL_ORDER)))}",
                is_error=True,
            )

        # Reading and parsing a multi-MB file is well over the 50 ms the event
        # loop can afford to lose, and this tool is most likely to be called
        # while something is already wrong.
        return await asyncio.to_thread(self._summarise, path, floor, inp)

    def _summarise(self, path: Path, floor: int, inp: LogTriageInput) -> ToolResult:
        records: list[_Record] = []
        unparsed = 0
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                hit = _RECORD.match(line)
                if hit is None:
                    # Continuation of the previous record (traceback body).
                    if records:
                        previous = records[-1]
                        if len(previous.message) < _MAX_CONTINUATION_CHARS:
                            previous.message += " " + line.strip()
                        elif not previous.truncated:
                            previous.message += " …[continuation truncated]"
                            previous.truncated = True
                    else:
                        unparsed += 1
                    continue
                try:
                    stamp = datetime.strptime(hit.group("stamp"), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    stamp = None
                records.append(
                    _Record(
                        stamp=stamp,
                        level=hit.group("level").upper(),
                        logger=hit.group("logger"),
                        message=hit.group("message").strip(),
                    )
                )

        if not records:
            return ToolResult(
                output=(
                    f"{path}: no parseable log records "
                    f"({unparsed} unrecognised lines). Expected "
                    "'YYYY-MM-DD HH:MM:SS,mmm LEVEL logger: message'."
                )
            )

        if inp.since_hours is not None:
            newest = max((r.stamp for r in records if r.stamp), default=None)
            if newest is not None:
                cutoff = newest - timedelta(hours=inp.since_hours)
                records = [r for r in records if r.stamp is None or r.stamp >= cutoff]

        stamps = [r.stamp for r in records if r.stamp]
        by_level = Counter(r.level for r in records)
        interesting = [r for r in records if _LEVEL_ORDER.get(r.level, 0) >= floor]
        by_logger = Counter(r.logger for r in interesting)

        grouped: dict[str, list[_Record]] = {}
        for record in interesting:
            grouped.setdefault(f"{record.level} {record.logger}: {normalise(record.message)}", []).append(record)

        ranked = sorted(grouped.items(), key=lambda kv: len(kv[1]), reverse=True)

        lines = [f"{path}", f"{len(records)} records"]
        if stamps:
            lines[-1] += f" spanning {min(stamps):%Y-%m-%d %H:%M} to {max(stamps):%Y-%m-%d %H:%M}"
        if inp.since_hours is not None:
            lines[-1] += f" (last {inp.since_hours}h)"
        if unparsed:
            lines.append(f"{unparsed} leading lines did not parse and were skipped")

        lines.append("")
        lines.append("by level: " + ", ".join(f"{lvl} {n}" for lvl, n in by_level.most_common()))

        if not interesting:
            lines.append("")
            lines.append(f"Nothing at {inp.min_level.upper()} or above. This log is clean by that measure.")
            return ToolResult(
                output="\n".join(lines),
                metadata={"records": len(records), "by_level": dict(by_level), "at_or_above_min_level": 0},
            )

        lines.append(
            f"by logger (>= {inp.min_level.upper()}): "
            + ", ".join(f"{name} {n}" for name, n in by_logger.most_common(10))
        )
        lines.append("")
        lines.append(f"{len(ranked)} distinct messages at {inp.min_level.upper()} or above, most frequent first:")
        for key, hits in ranked[: inp.top]:
            newest = max((h.stamp for h in hits if h.stamp), default=None)
            when = f", newest {newest:%Y-%m-%d %H:%M}" if newest else ""
            sample = hits[-1].message
            if len(sample) > 200:
                sample = sample[:200] + "…"
            lines.append(f"  {len(hits)}x{when} — {key.split(':', 1)[0]}")
            lines.append(f"      {sample}")
        if len(ranked) > inp.top:
            lines.append(f"  ... and {len(ranked) - inp.top} more distinct messages")

        return ToolResult(
            output="\n".join(lines),
            metadata={
                "records": len(records),
                "by_level": dict(by_level),
                "at_or_above_min_level": len(interesting),
                "distinct_messages": len(ranked),
                # A summary that silently dropped input would be the same class
                # of defect this tool exists to fix.
                "continuation_truncated": any(r.truncated for r in records),
            },
        )
