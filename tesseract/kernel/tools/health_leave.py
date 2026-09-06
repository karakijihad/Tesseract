"""Leaving a health finding alone, and picking it back up.

The Health room shows what the runtime can see, and some of what it can see is
true, understood, and not going to be acted on: a spare that is slow while a
key is being replaced, a row that refused once for a reason the operator
already knows. Until this there was no way to say so. The line stayed in Needs
action until the condition itself changed, so a fault they had read and decided
to live with went on demanding attention every time the panel was opened, and a
panel that keeps asking about something already answered teaches them to stop
reading it.

Operator, 2026-09-02: *"i need also the ability to change stuff to green if i
want."*

**A tool rather than a button, per ruling 26.** A control every surface can
reach is a tool: the operator is away from the desk when the runtime is at its
most autonomous, and a cockpit-only way to quiet a row is a way that does not
exist when they need it.

**Nothing is hidden by this.** The row stays on the panel, moves to Operating,
and says it was marked as seen and when. It returns to Needs action on its own
if the same fault gets worse than it was when they looked, and the
acknowledgement is spent when the fault stops being found, so the same thing
arriving next month is new again. Those three rules live in
`orchestrator/watchman/acknowledged.py` with the reasoning; this is the door
to them.

`default_posture="auto"`. It changes how one line is banded on a panel the
operator owns, decides nothing about what the runtime may do, and undoing it is
one call. A gate here would ask permission to stop being alarmed.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, ClassVar, Literal, Optional

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)

#: How many keys one refusal lists back. Enough to find the one meant, few
#: enough to read on a phone.
SUGGEST_ROWS = 12


class HealthLeaveInput(BaseModel):
    subject: str = Field(
        description=(
            "What the finding is about, exactly as the Health room names it: "
            "a provider ref like `api.openai.gpt56_luna`, a scheduled row like "
            "`capture`, a breaker name."
        )
    )
    action: Literal["leave", "restore"] = Field(
        default="leave",
        description=(
            "`leave` to stop it asking for attention. `restore` to undo that "
            "and have it count again."
        ),
    )
    note: str = Field(
        default="",
        description=(
            "Why, in the operator's own words. Shown on the row afterwards, "
            "because 'the key is being replaced on Friday' is worth more to "
            "whoever reads it next than the fact that somebody dismissed it."
        ),
    )


class HealthLeaveTool(Tool):
    default_posture = "auto"

    def __init__(self, app_provider: Optional[Callable[[], Any]] = None) -> None:
        # The room's rows, from the room's own reader. Without the backend
        # this still answers for everything the watchman found, because that
        # is a file on disk; what it cannot see is the collector rows, and it
        # says so rather than refusing a name it simply could not check.
        self._app_provider = app_provider

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "checking-your-state"
    summary: ClassVar[str] = (
        "Stop a Health finding asking for attention, or have it count again."
    )
    use_when: ClassVar[str] = (
        "Use when the operator says a health row is known, expected, or fine "
        "to leave: 'I know about that one', 'that is expected', 'stop showing "
        "me that'. Name the subject exactly as the room names it, and pass "
        "their reason as the note. Use `restore` when they want it back."
    )
    not_when: ClassVar[str] = (
        "to hide something they have not said is fine; for what is waiting on "
        "them to decide, which is `workspace_pending` and `workspace_decide`; "
        "for what the panel currently says, which is `autonomy_read`."
    )
    depends_on: ClassVar[str] = ""

    @property
    def name(self) -> str:
        return "health_leave"

    @property
    def input_schema(self) -> type[BaseModel]:
        return HealthLeaveInput

    def is_concurrency_safe(self) -> bool:
        return False

    async def run(self, args: BaseModel, context: ToolContext) -> ToolResult:
        from tesseract.orchestrator.watchman import acknowledged
        from tesseract.orchestrator.watchman.judge import standing

        inp = args if isinstance(args, HealthLeaveInput) else HealthLeaveInput(**args.model_dump())
        subject = inp.subject.strip()
        if not subject:
            return ToolResult(output="Name what to leave alone.", is_error=True)

        keys = [standing.key_for(f) for f in _findings_for(subject)]
        rows = await self._rows_for(subject)
        keys += [str(row["key"]) for row in rows]
        if not keys:
            known = _subjects() | {str(r["name"]) for r in await self._rows()}
            listed = ", ".join(sorted(known)[:SUGGEST_ROWS]) or "nothing"
            return ToolResult(
                output=(
                    f"The room says nothing about {subject!r}, so there is "
                    f"nothing to leave alone. What it does say: {listed}."
                ),
                is_error=True,
            )
        if inp.action == "restore":
            undone = [key for key in keys if acknowledged.forget(key)]
            if not undone:
                return ToolResult(
                    output=f"{subject} was already counting. Nothing changed."
                )
            return ToolResult(
                output=(
                    f"{subject} counts again and is back with whatever it was "
                    f"rated. {len(undone)} finding(s) restored."
                ),
                metadata={"subject": subject, "restored": undone},
            )

        for finding in _findings_for(subject):
            acknowledged.acknowledge(
                standing.key_for(finding), severity=finding.severity, note=inp.note
            )
        for row in rows:
            acknowledged.acknowledge(
                str(row["key"]), severity=_severity_of(row), note=inp.note
            )
        return ToolResult(
            output=(
                f"{subject} will stop asking for attention. It stays on the "
                f"panel under Operating, says you marked it as seen, and comes "
                f"back on its own if it gets worse or if it clears and returns."
            ),
            metadata={"subject": subject, "left": keys},
        )


    async def _rows(self) -> list[dict[str, Any]]:
        """Every row the Health room draws, from the room's own reader.

        The panel and this tool read one thing, so a row the operator can see
        is a row they can leave alone from anywhere, which is the whole of
        ruling 22. Without a backend it answers with the rows that come off
        disk and none of the ones that need the running app.
        """
        from datetime import datetime, timezone

        from tesseract.mirror.server.routes.autonomy_health import read_departments

        app = self._app_provider() if self._app_provider is not None else None
        if app is None:
            return []
        try:
            departments, _latest, _tail = await read_departments(
                app, datetime.now(timezone.utc), with_tail=False
            )
        except Exception:  # noqa: BLE001 — the sweep's findings still answer
            logger.exception("health_leave: the room's rows could not be read")
            return []
        return departments

    async def _rows_for(self, subject: str) -> list[dict[str, Any]]:
        """The collector rows this subject names.

        Findings are matched separately and by the judge's own key, so they
        are skipped here: a row that has one would otherwise be acknowledged
        under two keys and restored under one.
        """
        wanted = subject.casefold()
        return [
            row
            for row in await self._rows()
            if str(row.get("name", "")).casefold() == wanted
            and str(row.get("key", "")).startswith("collector//")
        ]


def _severity_of(row: dict[str, Any]) -> str:
    """What the store records a row as having been rated when it was left.

    The room's own reading, so "has it got worse" is asked in one vocabulary
    whether the row came from a finding or from a collector.
    """
    from tesseract.mirror.server.routes.autonomy_health import _SEVERITY_OF_STATE
    from tesseract.lib.log_envelope import INFO

    return _SEVERITY_OF_STATE.get(str(row.get("state")), INFO)


def _sweep() -> dict:
    """The last sweep, or an empty one. Read here rather than passed, so the
    tool answers against what the room is showing right now."""
    import json

    from tesseract.mirror.server.routes.autonomy_health import latest_path

    try:
        loaded = json.loads(latest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


class _Finding:
    """Just enough of a finding for `standing.key_for`, which takes attributes
    rather than a mapping. Restated instead of importing the dataclass because
    what is on disk is a dict and rebuilding the frozen class from it would
    mean carrying every field to use three."""

    def __init__(self, row: dict) -> None:
        self.source = str(row.get("source") or "")
        self.kind = str(row.get("kind") or "")
        self.subject = str(row.get("subject") or "")
        self.severity = str(row.get("severity") or "")


def _findings_for(subject: str) -> list[_Finding]:
    """Every finding about `subject`, matched case-insensitively.

    A subject can carry more than one finding at a time, and leaving one while
    the other still shouts would look like the control did nothing.
    """
    wanted = subject.casefold()
    return [
        _Finding(row)
        for row in (_sweep().get("findings") or [])
        if isinstance(row, dict) and str(row.get("subject") or "").casefold() == wanted
    ]


def _subjects() -> set[str]:
    return {
        str(row.get("subject") or "")
        for row in (_sweep().get("findings") or [])
        if isinstance(row, dict) and row.get("subject")
    }


__all__ = ["HealthLeaveInput", "HealthLeaveTool"]
