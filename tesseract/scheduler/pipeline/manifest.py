"""The run manifest — one row per stage, committed as each stage ends.

Two properties follow from committing after every stage, and both are
requirements rather than side effects: a hard kill costs one stage instead of a
night, and the next start knows exactly where the last one stopped. A manifest
written only at the end would answer neither question, which is the state the
scheduler is in today.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tesseract.orchestrator.outcome import RunOutcome
from tesseract.scheduler.pipeline.artifacts import atomic_write_json, pipeline_root

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StageRow:
    stage: str
    outcome: RunOutcome
    reason: str = ""
    changed: int = 0
    refused: int = 0
    reads: dict[str, int] = field(default_factory=dict)
    writes: dict[str, int] = field(default_factory=dict)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "changed": self.changed,
            "refused": self.refused,
            "reads": dict(self.reads),
            "writes": dict(self.writes),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_ms": round(self.duration_ms, 3),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "StageRow":
        def _ts(key: str) -> datetime | None:
            value = raw.get(key)
            if not isinstance(value, str) or not value:
                return None
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return None

        return cls(
            stage=str(raw["stage"]),
            outcome=RunOutcome(raw["outcome"]),
            reason=str(raw.get("reason") or ""),
            changed=int(raw.get("changed") or 0),
            refused=int(raw.get("refused") or 0),
            reads=dict(raw.get("reads") or {}),
            writes=dict(raw.get("writes") or {}),
            started_at=_ts("started_at"),
            ended_at=_ts("ended_at"),
            duration_ms=float(raw.get("duration_ms") or 0.0),
        )


@dataclass
class RunManifest:
    run_id: str
    anchor: datetime
    started_at: datetime
    rows: list[StageRow] = field(default_factory=list)
    # Stages whose cadence did not come due this run. Recorded rather than
    # dropped: "it did not run" and "it is not this run's business" are
    # different answers, and a health surface that cannot tell them apart is
    # the failure this plan started from.
    not_due: list[str] = field(default_factory=list)
    # Stages the operator turned off in the row's config block. Recorded
    # beside `not_due` rather than as a `refused` row: a refusal is something
    # that happened this run, and a setting changed once and forgotten is not.
    # As a row it made the whole nightly pass report `refused` forever.
    disabled: list[str] = field(default_factory=list)
    completed_at: datetime | None = None

    @property
    def committed(self) -> frozenset[str]:
        return frozenset(row.stage for row in self.rows)

    def outcome_of(self, stage: str) -> RunOutcome | None:
        for row in self.rows:
            if row.stage == stage:
                return row.outcome
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "anchor": self.anchor.isoformat(),
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "not_due": list(self.not_due),
            "disabled": list(self.disabled),
            "stages": [row.to_dict() for row in self.rows],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RunManifest":
        completed = raw.get("completed_at")
        return cls(
            run_id=str(raw["run_id"]),
            anchor=datetime.fromisoformat(raw["anchor"]),
            started_at=datetime.fromisoformat(raw["started_at"]),
            rows=[StageRow.from_dict(row) for row in raw.get("stages") or []],
            not_due=list(raw.get("not_due") or []),
            disabled=list(raw.get("disabled") or []),
            completed_at=datetime.fromisoformat(completed) if completed else None,
        )


class ManifestStore:
    """`runtime/pipeline/current.json` while a run is open; `runs/<id>.json`
    once it closes."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or pipeline_root()

    @property
    def open_path(self) -> Path:
        return self._root / "current.json"

    def run_path(self, run_id: str) -> Path:
        return self._root / "runs" / f"{run_id}.json"

    def load_open(self) -> RunManifest | None:
        """The manifest of a run that never finished, or None."""
        path = self.open_path
        if not path.exists():
            return None
        try:
            return RunManifest.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            log.exception(
                "pipeline manifest: unreadable open run at %s — starting a new run", path
            )
            return None

    def commit(self, manifest: RunManifest) -> None:
        atomic_write_json(self.open_path, manifest.to_dict())

    def finish(self, manifest: RunManifest) -> Path:
        manifest.completed_at = datetime.now(timezone.utc)
        path = self.run_path(manifest.run_id)
        atomic_write_json(path, manifest.to_dict())
        # Only the run that OWNS the open slot may clear it. A single stage run
        # by hand finishes the same way, and clearing unconditionally would
        # throw away the resume point of an interrupted nightly run — the one
        # thing this file exists to keep.
        open_manifest = self.load_open()
        if open_manifest is not None and open_manifest.run_id == manifest.run_id:
            try:
                self.open_path.unlink(missing_ok=True)
            except OSError:
                log.exception("pipeline manifest: could not clear %s", self.open_path)
        return path


class MemoryManifestStore:
    """A manifest that is never written down.

    For a row that fires in minutes there is nothing to resume — the next tick
    is five minutes away and redoes the window anyway — and a file per tick
    would be 288 of them a day. The run still produces a manifest, which
    `run_row` returns to the caller; this sink keeps nothing of its own.
    """

    def load_open(self) -> RunManifest | None:
        return None

    def commit(self, manifest: RunManifest) -> None:
        return None

    def finish(self, manifest: RunManifest) -> None:
        manifest.completed_at = datetime.now(timezone.utc)


__all__ = ["ManifestStore", "MemoryManifestStore", "RunManifest", "StageRow"]
