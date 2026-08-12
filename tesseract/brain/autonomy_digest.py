"""Autonomy -> chat cross-feed digest (lean-agent-os P1 Task 4).

Background thinking — open agenda items (the AU-5 kernel's queue) and
recent self-reflection observations (the ``autonomy_heartbeat`` scheduler
job) — currently only reaches the operator through dashboard polling or a
tool call the assistant has to think to make. This module renders a compact digest
of both into the "Right now" temporal block of the system prompt so that
awareness reaches every turn for free.

Pure function over injected readers: ``tesseract.brain.prompt`` wires the
live ``AgendaStore`` / conscience-memory readers; tests inject fakes so no
real ``TESSERACT_HOME`` store is needed. Readers are responsible for
filtering (which statuses count as "open", which memories are heartbeat
observations) and ordering (most relevant/most recent first) — this module
only caps, formats, and fails safe.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TypeVar

import yaml

from tesseract.paths import config_dir

logger = logging.getLogger(__name__)

MAX_AGENDA_ITEMS = 5
MAX_REFLECTIONS = 3
_WHITESPACE_RUN = re.compile(r"\s+")

_T = TypeVar("_T")


@dataclass(frozen=True)
class AutonomyDigestConfig:
    max_age_days: float


def _require(d: dict, key: str, where: str):
    if key not in d:
        raise RuntimeError(f"missing required key '{key}' in {where}")
    return d[key]


def load_autonomy_digest_config() -> AutonomyDigestConfig:
    """Read ``memory.yaml::autonomy_digest``; raise loudly on missing keys.

    Mirrors ``tesseract.brain.auto_recall.load_auto_recall_config``'s
    raise-loudly ``_require`` pattern — no ``.get(..., default)`` for an
    infrastructure value. The path resolves at call time for the same reason
    ``load_auto_recall_config`` does.
    """
    raw = yaml.safe_load((config_dir() / "memory.yaml").read_text(encoding="utf-8"))
    section = _require(raw, "autonomy_digest", "memory.yaml")
    return AutonomyDigestConfig(
        max_age_days=float(_require(section, "max_age_days", "memory.yaml autonomy_digest")),
    )


@dataclass(frozen=True)
class AgendaEntry:
    """One open agenda item. Caller's reader has already filtered to
    operator-relevant, non-terminal statuses and sorted by relevance."""

    title: str
    status: str
    created_at: datetime


@dataclass(frozen=True)
class ReflectionEntry:
    """One self-reflection observation. Caller's reader has already
    sorted newest-first."""

    text: str
    created_at: datetime


@dataclass(frozen=True)
class FailuresSnapshot:
    """P6 Task 3 §G4 — ambient failure signal for the digest.

    Caller's reader assembles this from live sources: tripped circuit
    breakers (``circuit_breaker.load_tripped_breakers``), the halt-watchdog's
    per-turn stall sweep, and the resume-time vanished-spawn journal sweep
    (``spawn_journal.sweep_orphans``, via ``failures_signal``). Counts are
    cumulative since this backend process booted ("for that boot") — not
    persisted; the underlying events already have durable evidence elsewhere
    (breaker JSONLs, the spawn journal).

    ``tool_error_streak`` (P6 Task 5 — escalate-on-failure reflex) is
    ``(tool_name, count)`` when the same tool has failed ≥2 times
    consecutively within a turn, ``None`` otherwise. Unlike the two counts
    above it is not cumulative — it reflects the last recorded streak and is
    cleared as soon as that tool succeeds (``failures_signal``).
    """

    tripped_breakers: Sequence[str]
    stalled_count: int
    vanished_count: int
    tool_error_streak: tuple[str, int] | None = None


AgendaReader = Callable[[], Sequence[AgendaEntry]]
ReflectionReader = Callable[[], Sequence[ReflectionEntry]]
UnvettedCountReader = Callable[[], int]
FailuresReader = Callable[[], FailuresSnapshot]


def render_digest(
    agenda_reader: AgendaReader,
    reflection_reader: ReflectionReader,
    *,
    unvetted_count_reader: UnvettedCountReader | None = None,
    failures_reader: FailuresReader | None = None,
    max_age_days: float | None = None,
    now: datetime | None = None,
) -> str:
    """Render the digest, or ``""`` when both sources are empty.

    Failure isolation: a reader raising, or a single entry failing to
    format, is caught and skipped (warning-logged) — never raised. This
    runs on every chat turn as part of prompt assembly; a corrupt agenda
    record or a missing conscience directory must never break the turn.

    ``max_age_days`` (lean-agent-os P1 follow-up, Q4 — see
    ``memory.yaml::autonomy_digest.max_age_days``), when given, excludes
    agenda entries older than that many days BEFORE the top-N cap below,
    so stale autonomy-generated noise (e.g. weeks-old ``resume_queued``
    items) doesn't crowd out fresh ones. ``None`` (the default) applies
    no filter — used by callers/tests that don't care about recency.
    Reflections are already capped at the 3 latest and are exempt.

    UNVETTED agenda items are never itemized by ``agenda_reader`` (they
    haven't cleared the vetter gate), but a non-zero
    ``unvetted_count_reader`` result adds one ``unvetted: N awaiting
    vetter`` line so the backlog isn't silently invisible. That line
    counts toward the 8-line budget (5 agenda + 3 reflection): when
    present, the agenda itemized cap drops from 5 to 4 so the total never
    exceeds 8 — the count line deterministically displaces the 5th
    agenda item rather than growing the budget.

    ``failures_reader`` (P6 Task 3 §G4, extended P6 Task 5), when given,
    appends at most ~4 ambient lines — tripped circuit breakers,
    stalled-spawn count, vanished-spawn count, consecutive same-tool error
    streak — after the reflections. Absent or all-empty → no lines, no
    extra section (same fail-quiet contract as the other readers).
    """
    clock = now or datetime.now(timezone.utc)

    unvetted_count = _safe_count(unvetted_count_reader) if unvetted_count_reader else 0
    agenda_cap = MAX_AGENDA_ITEMS - 1 if unvetted_count > 0 else MAX_AGENDA_ITEMS

    agenda_entries = _safe_read(agenda_reader, "agenda")
    if max_age_days is not None:
        agenda_entries = [
            entry for entry in agenda_entries
            if _within_max_age(entry.created_at, clock, max_age_days)
        ]

    lines: list[str] = []
    for entry in agenda_entries[:agenda_cap]:
        line = _format_agenda_line(entry, clock)
        if line:
            lines.append(line)

    if unvetted_count > 0:
        lines.append(f"unvetted: {unvetted_count} awaiting vetter")

    for entry in _safe_read(reflection_reader, "reflection")[:MAX_REFLECTIONS]:
        line = _format_reflection_line(entry, clock)
        if line:
            lines.append(line)

    if failures_reader is not None:
        lines.extend(_format_failures_lines(_safe_failures(failures_reader)))

    return "\n".join(lines)


def _safe_read(reader: Callable[[], Sequence[_T]], label: str) -> Sequence[_T]:
    try:
        return reader() or []
    except Exception:
        logger.warning("autonomy_digest: %s reader failed", label, exc_info=True)
        return []


def _safe_count(reader: UnvettedCountReader) -> int:
    try:
        return max(int(reader()), 0)
    except Exception:
        logger.warning("autonomy_digest: unvetted count reader failed", exc_info=True)
        return 0


def _safe_failures(reader: FailuresReader) -> FailuresSnapshot:
    try:
        snapshot = reader()
        return snapshot if snapshot is not None else FailuresSnapshot((), 0, 0)
    except Exception:
        logger.warning("autonomy_digest: failures reader failed", exc_info=True)
        return FailuresSnapshot((), 0, 0)


def _format_failures_lines(snapshot: FailuresSnapshot) -> list[str]:
    """At most 4 ambient failure lines — one per fact, only when non-empty."""
    lines: list[str] = []
    if snapshot.tripped_breakers:
        breakers = ", ".join(sorted(snapshot.tripped_breakers))
        lines.append(f"Failure: breaker tripped — {breakers}")
    if snapshot.stalled_count:
        lines.append(f"Failure: {snapshot.stalled_count} spawn(s) stalled")
    if snapshot.vanished_count:
        lines.append(f"Failure: {snapshot.vanished_count} spawn(s) vanished (backend restart)")
    if snapshot.tool_error_streak:
        tool_name, count = snapshot.tool_error_streak
        lines.append(f"Failure: {tool_name} failed {count}x consecutively last turn")
    return lines


def _sanitize(text: str) -> str:
    """Collapse all whitespace runs (incl. \\n \\r \\t) to a single space.

    Agenda titles / reflection text come from autonomy mappers and free-text
    summaries with a documented junk history. Without this, an embedded
    ``"\\n\\n"`` can forge a section break once this line is spliced into the
    system prompt (``prompt.py::_drop_block`` matches on literal
    ``"\\n\\n" + block``). Applied before any length bounding.
    """
    return _WHITESPACE_RUN.sub(" ", text).strip()


def _format_agenda_line(entry: AgendaEntry, clock: datetime) -> str:
    try:
        title = _sanitize(entry.title)
        age = _format_age(entry.created_at, clock)
        return f"Agenda: {title} · {entry.status} · {age}"
    except Exception:
        logger.warning("autonomy_digest: skipping malformed agenda entry", exc_info=True)
        return ""


def _format_reflection_line(entry: ReflectionEntry, clock: datetime) -> str:
    try:
        text = _sanitize(entry.text)
        age = _format_age(entry.created_at, clock)
        return f"Reflection: {text} · {age}"
    except Exception:
        logger.warning("autonomy_digest: skipping malformed reflection entry", exc_info=True)
        return ""


def _normalize_utc(value: datetime) -> datetime:
    """Naive ``datetime`` (no tzinfo) is treated as UTC — same precedent as
    ``agenda_store.py::AgendaStore.find_fuzzy_dedupe`` — so a naive record
    never raises ``TypeError`` when compared against a tz-aware clock.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _within_max_age(created_at: datetime, clock: datetime, max_age_days: float) -> bool:
    """``True`` when ``created_at`` is not older than ``max_age_days``.

    Inclusive at the boundary — an item exactly ``max_age_days`` old is
    "not older than" the cutoff, so it stays (matches "excluded when
    older than max_age_days" in the config comment). A malformed
    ``created_at`` (e.g. a non-datetime from a corrupt agenda record) is
    treated as fresh here and passed through; ``_format_agenda_line``'s
    own try/except is what skips it, same as before this filter existed.
    """
    try:
        created_at = _normalize_utc(created_at)
    except AttributeError:
        return True
    age_days = (clock - created_at).total_seconds() / 86400.0
    return age_days <= max_age_days


def _format_age(created_at: datetime, clock: datetime) -> str:
    """Age string for a timestamp. Naive ``created_at`` (no tzinfo) is
    treated as UTC — see ``_normalize_utc``.
    """
    created_at = _normalize_utc(created_at)
    delta = max((clock - created_at).total_seconds(), 0.0)
    if delta < 60:
        return f"{int(delta)}s"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    if delta < 86400:
        return f"{int(delta // 3600)}h"
    return f"{int(delta // 86400)}d"


__all__ = [
    "AgendaEntry",
    "AutonomyDigestConfig",
    "FailuresSnapshot",
    "ReflectionEntry",
    "MAX_AGENDA_ITEMS",
    "MAX_REFLECTIONS",
    "load_autonomy_digest_config",
    "render_digest",
]
