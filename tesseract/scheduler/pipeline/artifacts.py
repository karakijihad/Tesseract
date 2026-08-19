"""What the pipeline remembers between runs: artifact versions and watermarks.

A stage consumes a NAMED, versioned artifact — never "whatever is on disk now".
The version is what lets a run say which input it read, and the watermark is
what lets a stage that stopped early resume where it stopped instead of
re-reading the whole window.

Both live under `runtime/pipeline/` because they are machine-local resume
state: they describe how far this machine got, not what the operator knows.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tesseract.paths import runtime_dir

log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


def pipeline_root() -> Path:
    """`runtime/pipeline/` — manifests, artifact heads, watermarks."""
    return runtime_dir() / "pipeline"


def _check_name(name: str) -> str:
    if not _NAME_RE.match(name):
        raise ValueError(
            f"artifact name {name!r} must be lowercase [a-z0-9_.-] — it is "
            "also a filename"
        )
    return name


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write via tmp + `os.replace` so a crash mid-write leaves the previous
    version intact rather than half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


@dataclass(frozen=True)
class ArtifactVersion:
    name: str
    version: int
    produced_by: str
    produced_at: datetime
    watermark: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "produced_by": self.produced_by,
            "produced_at": self.produced_at.isoformat(),
            "watermark": self.watermark.isoformat() if self.watermark else None,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ArtifactVersion":
        produced_at = _parse_ts(raw.get("produced_at")) or datetime.now(timezone.utc)
        return cls(
            name=str(raw["name"]),
            version=int(raw["version"]),
            produced_by=str(raw.get("produced_by") or ""),
            produced_at=produced_at,
            watermark=_parse_ts(raw.get("watermark")),
        )


class ArtifactStore:
    """The head version of every named artifact, one JSON file each."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or (pipeline_root() / "artifacts")

    def _path(self, name: str) -> Path:
        return self._root / f"{_check_name(name)}.json"

    def head(self, name: str) -> ArtifactVersion | None:
        path = self._path(name)
        if not path.exists():
            return None
        try:
            return ArtifactVersion.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            log.exception("pipeline artifacts: unreadable head %s", path)
            return None

    def publish(
        self,
        name: str,
        *,
        produced_by: str,
        watermark: datetime | None = None,
    ) -> ArtifactVersion:
        current = self.head(name)
        version = ArtifactVersion(
            name=name,
            version=(current.version + 1) if current else 1,
            produced_by=produced_by,
            produced_at=datetime.now(timezone.utc),
            watermark=watermark,
        )
        atomic_write_json(self._path(name), version.to_dict())
        return version


class WatermarkStore:
    """How far each stage has consumed its input, in one file.

    Kept apart from the artifact heads because it answers a different
    question: an artifact's watermark describes the data, a stage's watermark
    describes the reader. A stage with no declared writes still has a
    position, and this is where it lives.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (pipeline_root() / "watermarks.json")

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.exception("pipeline watermarks: unreadable %s — starting fresh", self._path)
            return {}
        return raw if isinstance(raw, dict) else {}

    def get(self, stage: str) -> datetime | None:
        return _parse_ts(self._load().get(stage))

    def set(self, stage: str, when: datetime) -> None:
        data = self._load()
        data[stage] = when.isoformat()
        atomic_write_json(self._path, data)


__all__ = [
    "ArtifactStore",
    "ArtifactVersion",
    "WatermarkStore",
    "atomic_write_json",
    "pipeline_root",
]
