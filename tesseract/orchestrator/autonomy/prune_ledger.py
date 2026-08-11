"""Prune ledger — append-only record of admission-gate prunes.

Tracks every low-value/duplicate/malformed/capped proposal the autonomy
admission gate discards before it reaches the operator queue, so
recurrent-useless sources become visible. ``kernel.py::_persist_draft`` calls
``record_prune`` on all four discard branches — exact-duplicate, malformed,
fuzzy-duplicate and capped.

The file is SIZE-BOUNDED, not age-bounded, and that is deliberate: it is
appended to continuously, so its mtime is always fresh and an age-based
janitor rule would never fire on it. ``record_prune`` rolls it once past
``_MAX_BYTES``; ``read_prunes`` reads only the tail it needs.

Files land under ``<TESSERACT_HOME>/logs/autonomy/pruned.jsonl``. Path
resolves at every call so ``monkeypatch.setenv("TESSERACT_HOME", tmp_path)``
works without re-importing the module.

``record_prune`` is best-effort: a write error is logged and swallowed —
ledger availability must never block admission.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from tesseract.orchestrator.autonomy.models import AgendaSource
from tesseract.paths import log_dir

log = logging.getLogger(__name__)

# Roll at 2 MB, keeping one previous generation. Sized against the write rate
# rather than guessed: the highest-frequency branch is the exact-duplicate one
# in `_persist_draft`, and a record is ~250 bytes, so 2 MB is ~8k discards —
# far more history than the dashboard's 500-row view can use, and small enough
# that the tail read below never has to touch most of it.
_MAX_BYTES = 2 * 1024 * 1024
# Enough tail to satisfy `read_prunes(limit=500)` at ~250 bytes a record with
# generous headroom, without materialising a file that may be 2 MB.
_TAIL_BYTES = 512 * 1024
# Below this, the live file is assumed to have just rolled and the retained
# generation is read behind it.
#
# Derived from the read limit rather than set as a second free constant: the
# roll fires on BYTES and this fires on LINES, so as independent knobs they
# could be tuned into a state where the fallback is permanently on and every
# read pays for two files. `_DEFAULT_READ_LIMIT` is what the dashboard asks
# for, and `test_prune_ledger_bounds` asserts a full ledger holds comfortably
# more rows than this, which is the invariant that keeps the pair sane.
_DEFAULT_READ_LIMIT = 500
_MIN_TAIL_LINES = _DEFAULT_READ_LIMIT


class PruneStage(str, Enum):
    MALFORMED = "malformed"
    DUPLICATE = "duplicate"
    LOW_VALUE = "low_value"
    CAPPED = "capped"


class PruneRecord(BaseModel):
    """One pruned-proposal entry. ``goal`` is truncated to 500 chars on
    construction rather than raising — callers (Task 1.4) may pass the
    raw proposal goal without pre-truncating."""

    item_id: str | None = None
    source: AgendaSource
    goal: str = Field(max_length=500)
    stage: PruneStage
    reason: str = ""
    ts: datetime

    @field_validator("goal", mode="before")
    @classmethod
    def _truncate_goal(cls, value: str) -> str:
        if isinstance(value, str) and len(value) > 500:
            return value[:500]
        return value


def _pruned_path() -> Path:
    """Resolve ``<TESSERACT_HOME>/logs/autonomy/pruned.jsonl`` at call time.

    `log_dir` resolves the home itself, so this reads the environment through
    it rather than separately — two dead lines that computed a `home` nobody
    used were removed, since they implied an override path the code no longer
    expressed.
    """
    return log_dir("autonomy") / "pruned.jsonl"


def record_prune(rec: PruneRecord) -> None:
    """Append one JSON line to the prune ledger. Best-effort."""
    path = _pruned_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(rec.model_dump_json() + "\n")
        _roll_if_oversized(path)
    except OSError:
        log.warning("prune_ledger: append failed for %s", path)


def _roll_if_oversized(path: Path) -> None:
    """Keep the ledger bounded, replacing one previous generation.

    Age-based pruning cannot work here — the file is appended to constantly,
    so its mtime is always fresh and a janitor age rule would never fire on
    it. Best-effort like the append it follows: a failed roll costs disk,
    never an admission decision.
    """
    try:
        if path.stat().st_size <= _MAX_BYTES:
            return
        previous = path.with_suffix(path.suffix + ".1")
        previous.unlink(missing_ok=True)
        path.rename(previous)
    except OSError:
        log.warning("prune_ledger: roll failed for %s", path)


def _tail_lines(path: Path) -> list[str]:
    """The end of the ledger, without materialising the whole file.

    `read_prunes` only ever returns its newest `limit` rows and the dashboard
    asks for 500, so reading the entire ledger to answer that — twice per
    request, since `prune_counts` calls it again — was the cost this avoids.
    A partial first line is dropped: the read window can land mid-record.
    """
    lines = _tail_of_file(path)
    # A roll leaves the live file nearly empty while a whole generation sits
    # in `.1`. Without this the dashboard would blank at an arbitrary moment
    # and `prune_counts` would under-report its own window — the discontinuity
    # reading as "the noisy source stopped" rather than "the file rolled".
    previous = path.with_suffix(path.suffix + ".1")
    if len(lines) < _MIN_TAIL_LINES and previous.exists():
        lines = _tail_of_file(previous) + lines
    return lines


def _tail_of_file(path: Path) -> list[str]:
    """The last `_TAIL_BYTES` of one file, as whole lines.

    A partial first line is dropped: the read window lands mid-record.
    """
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - _TAIL_BYTES)
            fh.seek(start)
            chunk = fh.read()
    except OSError:
        return []
    lines = chunk.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    return lines


def read_prunes(*, limit: int = _DEFAULT_READ_LIMIT) -> list[PruneRecord]:
    """Return up to ``limit`` records, newest first. Missing file → ``[]``.
    Malformed lines are skipped defensively."""
    path = _pruned_path()
    if not path.exists():
        return []
    records: list[PruneRecord] = []
    try:
        for raw in _tail_lines(path):
            line = raw.strip()
            if not line:
                continue
            try:
                records.append(PruneRecord.model_validate_json(line))
            except ValueError:
                log.warning("prune_ledger: skip malformed line in %s", path)
    except OSError:
        log.warning("prune_ledger: read failed for %s", path)
        return []
    records.reverse()
    return records[:limit]


def prune_counts(*, window_hours: int) -> dict[str, dict[str, int]]:
    """Bucket records within the last ``window_hours`` as
    ``{source_value: {stage_value: count}}``. Missing file → ``{}``.

    Counts what the ledger tail holds, not the whole file — at ~250 bytes a
    record the tail covers thousands, far past any window this is asked for,
    and reading the entire ledger to answer a 24h question was never worth it.
    """
    path = _pruned_path()
    if not path.exists():
        return {}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    counts: dict[str, dict[str, int]] = {}
    # No limit argument: the tail is the bound now, and passing a huge number
    # here read as "deliberately load everything" — the behaviour this module
    # was just changed to stop doing.
    for rec in read_prunes():
        ts = rec.ts if rec.ts.tzinfo else rec.ts.replace(tzinfo=timezone.utc)
        if ts < cutoff:
            continue
        stage_counts = counts.setdefault(rec.source.value, {})
        stage_counts[rec.stage.value] = stage_counts.get(rec.stage.value, 0) + 1
    return counts
