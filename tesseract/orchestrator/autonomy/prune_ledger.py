"""Prune ledger — append-only record of admission-gate prunes.

Tracks every low-value/duplicate/malformed/capped proposal the (future)
autonomy admission gate discards before it reaches the operator queue, so
recurrent-useless sources become visible. This module owns the ledger
storage and read helpers only; wiring into the kernel is a later task.

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
from tesseract.paths import TESSERACT_HOME

log = logging.getLogger(__name__)


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
    """Resolve ``<TESSERACT_HOME>/logs/autonomy/pruned.jsonl`` at call time."""
    override = os.environ.get("TESSERACT_HOME")
    home = Path(override).resolve() if override else TESSERACT_HOME
    return home / "logs" / "autonomy" / "pruned.jsonl"


def record_prune(rec: PruneRecord) -> None:
    """Append one JSON line to the prune ledger. Best-effort."""
    path = _pruned_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(rec.model_dump_json() + "\n")
    except OSError:
        log.warning("prune_ledger: append failed for %s", path)


def read_prunes(*, limit: int = 500) -> list[PruneRecord]:
    """Return up to ``limit`` records, newest first. Missing file → ``[]``.
    Malformed lines are skipped defensively."""
    path = _pruned_path()
    if not path.exists():
        return []
    records: list[PruneRecord] = []
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
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
    ``{source_value: {stage_value: count}}``. Missing file → ``{}``."""
    path = _pruned_path()
    if not path.exists():
        return {}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    counts: dict[str, dict[str, int]] = {}
    for rec in read_prunes(limit=1_000_000):
        ts = rec.ts if rec.ts.tzinfo else rec.ts.replace(tzinfo=timezone.utc)
        if ts < cutoff:
            continue
        stage_counts = counts.setdefault(rec.source.value, {})
        stage_counts[rec.stage.value] = stage_counts.get(rec.stage.value, 0) + 1
    return counts
