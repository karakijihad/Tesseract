"""Explaining a Health row well enough to act on it, without acting on it.

The Health room could report a fault and it could mute one. Between those two
buttons there was nothing: no way to hand the assistant the row it is looking
at and have it come back with what the row means, what has already been tried,
and which of the runtime's own tools bear on it. The operator was the one who
had to translate a room line into `breaker_status`, or `schedule_run`, or the
crash-storm script, every time, from memory.

**This tool does not repair anything.** It reads the row the same way the room
itself does, and returns the evidence packet the assistant needs in order to
go fix it with its ORDINARY tools, through the ORDINARY permission gate. Every
tool it points at is gated on its own; naming `breaker_reset` here asks
nothing and runs nothing. `default_posture = "auto"` follows from that:
packaging what is already on the operator's own panel decides nothing, and
undoing a bad read costs nothing either.

**The packet goes to a model, and that changes what may be in it.** A
finding's evidence can carry a request URL, a server's own response body, or a
raw `repr(exc)` this runtime did not choose to say — see
`orchestrator/watchman/findings.py` on `quotable` and `model_summary`. The room
itself already draws this line once, in `department()`'s `saidToModel` field:
a row's own composed sentence when the runtime built it, empty when nothing
was safe to forward. This tool reads that field and never the room's `said`,
which is the operator's copy and may carry text this runtime was handed.

The persisted sweep (`autonomy/watchman/latest.json`) goes further than
`saidToModel` already covers: it never carries a finding's raw evidence lines
or its `quotable` flag at all, only the coalesced summary and the counted
facts. So this tool cannot quote an evidence line even by accident, because
it never has one to quote.

What it says about evidence is therefore a COUNT and nothing else. Not the
lines, and not the file either. Naming the file was the original design and
an audit was right that it was a way around the gate rather than a
convenience: `report.write_evidence` fills that file with `finding.evidence`
unconditionally, ungated by `quotable`, because the file is written for the
operator; `_path_anchor.anchor_read_path` deliberately exempts absolute paths
from containment; and `log_triage` reads them. Three decisions that are each
right alone and together made a door. A row with no filed report (anything
short of the worst severity) says so plainly rather than guessing at a file
that was never written.
"""

from __future__ import annotations

import logging
import re
from hashlib import sha1
from pathlib import Path
from typing import Any, Callable, ClassVar, Optional

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.lib.log_envelope import BAD

logger = logging.getLogger(__name__)

#: How many keys or names one refusal lists back. Same bar `health_leave`
#: uses: enough to find the one meant, few enough to read on a phone.
SUGGEST_ROWS = 12

#: The watchman's own source names, mapped to which of the runtime's tools can
#: look into a row from that source. Read against `department()`'s row, never
#: declared a second time: a finding's `source` field decides this, and a
#: collector row (no `source` at all) falls to the name-based cases below or
#: to the catch-all.
_BREAKER_SOURCE = "circuit-breakers"
_SCHEDULE_SOURCE = "schedule"
_PIPELINE_SOURCE = "pipeline"
_PROVIDER_SOURCE = "provider-health"
_BACKEND_SOURCE = "backend"

#: The one row name crash-storm protection is filed under
#: (`autonomy_health.from_crash_storm`). Named rather than matched by source,
#: because the marker it reads has no watchman source at all — it is a file
#: the supervisor itself writes.
_CRASH_STORM_NAME = "crash storm"


class HealthRepairInput(BaseModel):
    key: str = Field(
        default="",
        description=(
            "The row's exact identity, as the panel carries it (its `key` "
            "field). Selects exactly one row, findings whose subject is "
            "empty included. Use this when you have it."
        ),
    )
    subject: str = Field(
        default="",
        description=(
            "What the row is about, exactly as the Health room names it, "
            "case-insensitive. Every row with that name. Use this when you "
            "only have the words a person typed."
        ),
    )
    request: str = Field(
        default="",
        description=(
            "What the operator actually asked for, in their own words. "
            "Echoed back for your own reference; changes nothing about what "
            "is read."
        ),
    )

    def model_post_init(self, __context: Any) -> None:
        self.key = self.key.strip()
        self.subject = self.subject.strip()


class HealthRepairTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "checking-your-state"
    summary: ClassVar[str] = "Explain one Health row and name the tools that can look into it. Fixes nothing itself."
    use_when: ClassVar[str] = (
        "Use when the operator points at a Health row and wants it looked "
        "into: 'look into it', 'what's wrong with that one', 'why is this "
        "red'. Name the row's `key` when you have it (the room's own "
        "identity for it), or its subject exactly as the room names it "
        "otherwise. Read the packet back, then act with the tool it names "
        "through the ordinary gate."
    )
    not_when: ClassVar[str] = (
        "to actually clear, retry, or fix anything, which this never does; "
        "call the tool the packet names instead. For what the panel "
        "currently says without one row picked out, use `autonomy_read`. To "
        "stop a row asking for attention, or put it back, use `health_leave`."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "none"
    recovery_behaviour: ClassVar[str] = "read_only"

    def __init__(self, app_provider: Optional[Callable[[], Any]] = None) -> None:
        self._app_provider = app_provider

    @property
    def name(self) -> str:
        return "health_repair"

    @property
    def input_schema(self) -> type[BaseModel]:
        return HealthRepairInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    async def run(self, args: BaseModel, context: ToolContext) -> ToolResult:
        from tesseract.orchestrator.watchman.judge import standing

        inp = args if isinstance(args, HealthRepairInput) else HealthRepairInput(**args.model_dump())
        if bool(inp.key) == bool(inp.subject):
            return ToolResult(
                output=(
                    "Name one row: pass its `key`, or its `subject`, never "
                    "both and never neither."
                ),
                is_error=True,
                caller_error=True,
            )

        departments = await self._rows()
        if inp.key:
            matches = [row for row in departments if str(row.get("key") or "") == inp.key]
            if not matches:
                known = sorted({str(row.get("key") or "") for row in departments})
                listed = ", ".join(known[:SUGGEST_ROWS]) or "nothing, the room has no rows right now"
                return ToolResult(
                    output=(
                        f"No row on the panel carries the key {inp.key!r}. "
                        f"Keys it has right now: {listed}."
                    ),
                    is_error=True,
                    caller_error=True,
                )
        else:
            wanted = inp.subject.casefold()
            matches = [
                row for row in departments if str(row.get("name") or "").casefold() == wanted
            ]
            if not matches:
                known = sorted({str(row.get("name") or "") for row in departments})
                listed = ", ".join(known[:SUGGEST_ROWS]) or "nothing, the room has no rows right now"
                return ToolResult(
                    output=(
                        f"The room says nothing called {inp.subject!r}. What "
                        f"it does say: {listed}."
                    ),
                    is_error=True,
                    caller_error=True,
                )

        latest = _read_latest_sweep()
        raw_by_key = _raw_findings_by_key(latest)
        try:
            stood = standing.load()
        except Exception:  # noqa: BLE001 — a missing history is not a missing answer
            logger.exception("health_repair: the standing-fault store could not be read")
            stood = {}

        packets = [
            _packet_for(row, raw_by_key.get(str(row.get("key") or "")), stood)
            for row in matches
        ]
        header = (
            f"{len(packets)} rows carry that name on the panel right now.\n\n"
            if len(packets) > 1
            else ""
        )
        output = header + "\n\n".join(packets)
        output += (
            "\n\nThis only reads the row. Nothing has been changed. Use the "
            "tool named above, through the ordinary gate, to actually act."
        )
        if inp.request:
            output += f"\n\nWhat was asked: {inp.request}"

        return ToolResult(
            output=output,
            metadata={"matched_keys": [str(row.get("key") or "") for row in matches]},
        )

    async def _rows(self) -> list[dict[str, Any]]:
        """Every row the Health room draws, from the room's own reader.

        Same source `health_leave` reads, and for the same reason: the room
        and this tool answer about one thing, and two readers of it can
        disagree about the same day. Without a running app this answers []
        rather than guessing, and the caller reports that as "the room has no
        rows right now" instead of failing.
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
        except Exception:  # noqa: BLE001 — the tool says less rather than raising
            logger.exception("health_repair: the room's rows could not be read")
            return []
        return departments


def _read_latest_sweep() -> dict[str, Any]:
    """The last sweep's raw dict, or `{}` when there is none to read.

    Read through the room's own function rather than a second file open: one
    reader of `latest.json`, same as everywhere else in this family of tools.
    """
    from tesseract.mirror.server.routes.autonomy_health import read_latest

    loaded = read_latest()
    return loaded if isinstance(loaded, dict) else {}


def _finding_key(raw: dict[str, Any]) -> str:
    """A raw finding's identity, in the exact words `standing.key_for` and the
    room's own `_key_of` both use. Restated rather than imported: both live in
    files this tool does not own, and a third copy of a three-field format
    string is a smaller risk than a coupling to either one's internals."""
    return "{}/{}/{}".format(
        raw.get("source") or "", raw.get("kind") or "", raw.get("subject") or ""
    )


def _raw_findings_by_key(latest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        _finding_key(row): row
        for row in (latest.get("findings") or [])
        if isinstance(row, dict)
    }


def _packet_for(
    row: dict[str, Any], raw: dict[str, Any] | None, stood: dict[str, Any]
) -> str:
    """Everything this tool knows about one row, in words safe for a model.

    `row` is the room's own department dict: name, key, band, state, label,
    obligation, said (operator's copy, never used here), saidToModel (the
    forwardable copy, already gated at the source — `department()`'s own
    `saidToModel`, which for a finding is `model_summary` already coalesced
    onto `summary` and for a collector row is the sentence the reader
    composed itself or nothing). `raw` is the same finding from the sweep's
    own JSON when this is a finding-derived row, `None` for a collector row.
    """
    key = str(row.get("key") or "")
    name = str(row.get("name") or "")
    is_collector = key.startswith("collector//")
    lines = [
        f"Row: {name}",
        f"key: {key}",
        f"state: {row.get('label') or row.get('state') or 'unknown'} ({row.get('band') or 'unbanded'})",
        f"asks: {row.get('obligationLabel') or row.get('obligation') or 'nothing recorded'}",
        f"left alone: {'yes' if row.get('acknowledged') else 'no'}",
    ]
    said_to_model = str(row.get("saidToModel") or "")
    if said_to_model:
        lines.append(f"what it says: {said_to_model}")
    else:
        # NOT "open the room itself". That sentence told the model where to go
        # around the gate this branch exists to hold, and an audit found the
        # way through: `autonomy_read` is `auto` and its line-builder preferred
        # `said` over `saidToModel`, so the room would have answered with the
        # operator's copy of the row. That reader is fixed, and pointing at it
        # is still the wrong instinct: the operator is who reads text this
        # runtime was handed, and asking them is a turn rather than a hole.
        lines.append(
            "what it says: nothing here is safe to repeat to you; it carries "
            "words this runtime did not choose. Ask the operator what the row "
            "says if you need it."
        )

    at = row.get("at")
    if is_collector or raw is None:
        lines.append(f"last seen: {at or 'unknown'}")
        lines.append(
            "this row is not about one named subject; it is the reading itself."
        )
        lines.append(_tools_for(name=name, source=""))
        return "\n".join(lines)

    subject = str(raw.get("subject") or "")
    if subject:
        lines.append(f"about: {subject}")
    else:
        lines.append(
            "about: nothing named. An empty subject means this is about the "
            "runtime as a whole, not one thing inside it."
        )
    count = raw.get("count") or row.get("value") or "1"
    lines.append(f"seen {count} time(s), last at {at or 'unknown'}")
    standing_entry = stood.get(key)
    if standing_entry is not None:
        lines.append(f"first seen: {standing_entry.first_reported.isoformat()}")
    else:
        lines.append("first seen: not on record; this may be its first sweep.")

    source = str(raw.get("source") or "")
    kind = str(raw.get("kind") or "")
    severity = str(raw.get("severity") or "")
    summary_for_evidence = str(raw.get("summary") or "")
    lines.append(_evidence_note(source=source, kind=kind, summary=summary_for_evidence, severity=severity))
    lines.append(_tools_for(name=name, source=source))
    return "\n".join(lines)


def _tools_for(*, name: str, source: str) -> str:
    """Which of the runtime's own tools bear on a row of this kind.

    Crash-storm protection is the one exception named outright: it has no
    breaker (`kernel/tools/breaker_reset.py` says so in its own `not_when`),
    so pointing at `breaker_reset` here would send the assistant at a tool
    that will refuse it.
    """
    if name.casefold() == _CRASH_STORM_NAME:
        return (
            "Tools: none of them. Crash-storm protection is not a breaker; "
            "`breaker_reset` will not touch it. Clear it from a shell with "
            "`python -m tesseract.scripts.clear_crash_storm`, once you know "
            "why it kept stopping."
        )
    if source == _BREAKER_SOURCE:
        return "Tools: breaker_status to see what it says, then breaker_reset once the cause is gone."
    if source == _SCHEDULE_SOURCE:
        return "Tools: schedule_list to see the row, then schedule_run to fire it again now."
    if source == _PIPELINE_SOURCE:
        return "Tools: pipeline_run_stage, to run that step of the daily pass again by hand."
    if source == _PROVIDER_SOURCE:
        return "Tools: system_diagnose, to see what this machine can currently reach."
    if source == _BACKEND_SOURCE:
        return "Tools: log_triage, to read what the backend log said around this."
    return "Tools: system_diagnose and log_triage; between them, a machine check and the logs."


def _evidence_note(*, source: str, kind: str, summary: str, severity: str) -> str:
    """How many evidence lines the last sweep filed for this. Not where.

    Never the lines themselves, and since an audit found the hole, never the
    path either. The reasoning this was built on was half right: the sweep
    really does not carry the lines (`report.py::_write_latest` persists
    neither `evidence` nor `quotable`), so nothing quotable reaches this
    function. But `report.write_evidence` writes `finding.evidence` into its
    own file UNCONDITIONALLY, ungated by `quotable`, because that file is the
    operator's and the gate governs what is narrated to a model rather than
    what is kept on disk.

    So naming the file and telling the reader to open it handed the model a
    door around the gate. `_path_anchor.anchor_read_path` exempts absolute
    paths from containment on purpose, `log_triage` reads them, and the
    backend collector's evidence lines are raw log lines that `log_triage`'s
    own record pattern parses and quotes straight back. Three deliberate
    decisions, each fine alone.

    The count still tells the assistant whether there is more to know. Whether
    the operator shows it is the operator's to decide, and asking them is the
    only route offered. Not the room: its Report section draws the sweep's own
    summary document, which is a different file from the per-finding evidence
    counted here, so sending a reader there would be pointing confidently at
    the wrong artifact.
    """
    if severity != BAD:
        return (
            "evidence: the last sweep did not file a separate report for "
            "this; the line above is everything it recorded."
        )
    from tesseract.orchestrator.watchman.report import evidence_dir

    slug = re.sub(r"[^a-z0-9]+", "-", f"{source}-{kind}".lower()).strip("-")
    fingerprint = sha1(summary.encode("utf-8")).hexdigest()[:8]
    try:
        found = sorted(evidence_dir().glob(f"*-{slug}-{fingerprint}.md"))
    except OSError:
        found = []
    if not found:
        return (
            "evidence: the last sweep should have filed a report for this "
            "and it is not where that would put it."
        )
    # Not "on the Health room". The room's Report section shows the sweep's own
    # summary document (`read_report` reads `latest['summary_path']`), and the
    # per-finding evidence this counts is a different file that
    # `report.write_evidence` puts under `evidence_dir()` and the room never
    # surfaces. Sending a reader to the wrong artifact is worse than sending
    # them nowhere, so this says who to ask instead of where to look.
    return (
        f"evidence: {_count_evidence_lines(found[-1])} line(s) were filed for "
        "the operator. They are not repeated here and the file is not named, "
        "because those lines can carry text the runtime was handed rather "
        "than text it wrote. Ask the operator what they say if the count "
        "suggests they matter."
    )


def _count_evidence_lines(path: Path) -> int:
    """How many lines sit in the fenced block `render_evidence` writes.

    Opens the file to count, never to quote. The placeholder line
    `render_evidence` writes when a finding carried nothing counts as zero,
    not one.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    lines = text.splitlines()
    try:
        start = lines.index("```") + 1
        end = lines.index("```", start)
    except ValueError:
        return 0
    body = lines[start:end]
    if body == ["(the source carried no quotable line)"]:
        return 0
    return len(body)


__all__ = ["HealthRepairInput", "HealthRepairTool"]
