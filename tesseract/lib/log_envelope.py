"""One shape for every record this runtime writes down.

Sixteen streams were inventoried and they agree on nothing. Six spellings of
"when": `ts`, `timestamp`, `at_utc`, `probed_at`, `fired_at`, `started_at_utc`.
Zero streams saying how bad a line is in a way another stream would recognise.
Zero declaring what a record is ABOUT. So every reader invents an identity per
source, decides severity per source, and the report cannot group because
nothing in the record says two lines are one thing.

Eight fields, and a writer that refuses a record missing one:

    ts        one spelling, UTC, always
    stream    who wrote it
    severity  info, warn or bad, decided at the source
    subject   what it is about, matchable across ticks
    summary   one line this runtime composed
    cause     records sharing one incident share this
    detail    free-form, never shown to a model
    outcome   only when the record is about a run

**`severity` is decided by the writer and nobody else.** The writer knows
whether a probe that came back slow is a problem on this machine; a reader
three subsystems away is guessing, and every one of those guesses used to be a
separate `if source == ...` branch.

**`cause` is what `subject` cannot do.** Subject answers "is this the same
thing as last hour", which is what the judge needs. It does not answer "are
these four records one problem", which is what a report needs, and four errors
from four loggers have four subjects and one cause.

**`kind` is deliberately absent.** It was in the first draft and no consumer
branches on anything `severity` plus `subject` does not already serve. It gets
added when something needs it, not before.

## Reading

`read_when` and `read_envelope` are the other half, and they are why no record
at rest is ever rewritten. A record written before the envelope existed is
recognised by its own fields and read into the same shape, so old files age
out under retention instead of being edited. Editing evidence is the one thing
this is not allowed to do.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

INFO = "info"
WARN = "warn"
BAD = "bad"
SEVERITIES = (INFO, WARN, BAD)

# Every spelling of "when" this tree has ever written. Ordered, not a set,
# and the order carries two decisions.
#
# The envelope's `ts` leads, so a row that has gained it while keeping its
# older key resolves to the envelope rather than to whichever key a reader
# happened to try first.
#
# After that the order is the RECORD'S OWN MOMENT. Two streams write both ends
# of theirs, and a janitor sweep belongs to when it finished and a scheduled
# run to when it completed: that is the moment the outcome became true, and it
# is the one both readers used before the envelope existed.
WHEN_KEYS = (
    "ts",
    "timestamp",
    "at_utc",
    "probed_at",
    "finished_at_utc",
    "completed_at",
    "fired_at",
    "started_at_utc",
    "decided_at",
)

# What a legacy row calls the thing it is about, per the inventory. Same rule:
# ordered, and the envelope's own key wins.
SUBJECT_KEYS = (
    "subject",
    "ref",
    "job_name",
    "breaker",
    "tool",
    "name",
    "provider",
    "source",
    "session_id",
    "author_id",
)


class EnvelopeError(ValueError):
    """A record that does not carry the envelope.

    Raised at the WRITE, not swallowed, because a producer emitting a record
    nothing can group is a fault in the producer and there is no run-time
    recovery from it. Every caller builds its envelope from values it already
    holds, so this can only fire on a coding mistake and it fires in a test
    before it fires anywhere else.
    """


def envelope(
    *,
    stream: str,
    severity: str,
    summary: str,
    subject: str = "",
    cause: str = "",
    detail: dict[str, Any] | None = None,
    outcome: str = "",
    ts: datetime | None = None,
) -> dict[str, Any]:
    """The eight fields, validated, ready to be one JSON line.

    `ts` defaults to now and is always stored in UTC. A writer that wants its
    file readable in local time keeps its own local-zone field beside this
    one: the envelope's job is to be comparable across streams, and a local
    offset is comparable only if every reader remembers to convert.
    """
    if not stream.strip():
        raise EnvelopeError("a record must say which stream wrote it")
    if severity not in SEVERITIES:
        raise EnvelopeError(
            f"severity {severity!r} is not one of {', '.join(SEVERITIES)}"
        )
    if not summary.strip():
        raise EnvelopeError(f"{stream}: a record must carry a summary line")
    when = ts or datetime.now(timezone.utc)
    if when.tzinfo is None:
        raise EnvelopeError(
            f"{stream}: a naive timestamp cannot be compared with another "
            "stream's. Pass an aware datetime."
        )
    return {
        "ts": when.astimezone(timezone.utc).isoformat(),
        "stream": stream,
        "severity": severity,
        "subject": subject,
        "summary": summary.strip(),
        "cause": cause,
        "detail": detail or {},
        "outcome": outcome,
    }


@dataclass(frozen=True)
class Record:
    """One row, read into the envelope whether or not it was written in it."""

    ts: datetime | None
    stream: str
    severity: str
    subject: str
    summary: str
    cause: str
    detail: dict[str, Any]
    outcome: str
    # True when the row carried the envelope itself. A reader that wants to
    # know how much it had to infer asks this rather than guessing from
    # whether a field came back empty.
    declared: bool


def read_when(row: dict[str, Any]) -> datetime | None:
    """The record's moment, in whichever of the spellings it was written.

    Naive timestamps are read as UTC rather than dropped. A log line with no
    zone is still evidence, and dropping it silently would make an old failure
    look new.
    """
    for key in WHEN_KEYS:
        if key in row and (parsed := parse_ts(row[key])) is not None:
            return parsed
    return None


def parse_ts(value: Any) -> datetime | None:
    """One ISO 8601 reader. `Z`, `+00:00` and a local offset all appear."""
    if not value:
        return None
    if isinstance(value, datetime):
        return (
            value.astimezone(timezone.utc)
            if value.tzinfo
            else value.replace(tzinfo=timezone.utc)
        )
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


def read_envelope(row: dict[str, Any], *, stream: str) -> Record:
    """One row of any age, read into one shape.

    `stream` is what the caller knows from the file it opened, and it is only
    used when the row does not name itself. A record that declares a different
    stream than the directory it sits in is believed: it was written by
    whoever wrote it, and the directory is a filing decision.
    """
    declared = "severity" in row and "summary" in row
    return Record(
        ts=read_when(row),
        stream=str(row.get("stream") or stream),
        severity=str(row.get("severity") or "").strip() or _infer_severity(row),
        subject=_read_subject(row),
        summary=str(row.get("summary") or "").strip(),
        cause=str(row.get("cause") or ""),
        detail=row.get("detail") if isinstance(row.get("detail"), dict) else {},
        outcome=str(row.get("outcome") or ""),
        declared=declared,
    )


def _read_subject(row: dict[str, Any]) -> str:
    for key in SUBJECT_KEYS:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _infer_severity(row: dict[str, Any]) -> str:
    """What a record written before `severity` existed would have declared.

    Deliberately thin. Three shapes cover every legacy row the inventory
    found, and a fourth guess would be this module inventing a fact about a
    stream it does not own. Anything else reads as `info`, which is what every
    reader assumed about these rows anyway.
    """
    if row.get("ok") is False or row.get("errors"):
        return BAD
    outcome = str(row.get("outcome") or "").lower()
    if outcome in {"failed", "refused"}:
        return BAD
    if outcome == "degraded" or row.get("drift_kind"):
        return WARN
    return INFO


__all__ = [
    "BAD",
    "INFO",
    "SEVERITIES",
    "SUBJECT_KEYS",
    "WARN",
    "WHEN_KEYS",
    "EnvelopeError",
    "Record",
    "envelope",
    "parse_ts",
    "read_envelope",
    "read_when",
]
