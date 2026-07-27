"""Shutdown-intent file shape + atomic read/write.

The backend writes ``<TESSERACT_HOME>/runtime/intent.json`` before any
orderly exit. The supervisor reads it after backend process exit and
routes per the table in ``_shared/kill-switch-protocol.md``:

* ``operator_quit`` → exit zero; no respawn.
* ``restart_upgrade`` → respawn backend with ``TESSERACT_RESUME_CONTINUATION``.
* ``crash`` (default; intent absent or stale) → backoff + respawn.

Atomic write is tmp-file + rename so a partially-flushed file is never
visible to the reader. Staleness is detected via ``backend_pid`` +
``timestamp`` older than the supervisor-recorded backend start time.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


IntentKind = Literal["operator_quit", "restart_upgrade", "crash"]
IntentSource = Literal[
    "ui_button",
    "cli_tool",
    "supervisor_signal",
    "upgrade_manager",
    "health_timeout",
    "backend_signal",
]


class IntentFile(BaseModel):
    """The shape of ``intent.json``.

    Frozen so callers can't mutate after construction. ``continuation_id``
    is required for ``restart_upgrade`` and optional otherwise — the
    validation runs on the routing side (`Supervisor`), not the model.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    intent: IntentKind
    timestamp: datetime
    source: IntentSource
    continuation_id: str | None = None
    reason: str = ""
    backend_pid: int | None = None

    def to_payload(self) -> dict:
        d = self.model_dump()
        d["timestamp"] = self.timestamp.isoformat()
        # Drop empty optional fields so the on-disk shape stays tight.
        if not d.get("continuation_id"):
            d.pop("continuation_id", None)
        if not d.get("reason"):
            d.pop("reason", None)
        if d.get("backend_pid") is None:
            d.pop("backend_pid", None)
        return d

    @classmethod
    def from_payload(cls, payload: dict) -> "IntentFile":
        ts = payload.get("timestamp")
        if isinstance(ts, str):
            payload = {**payload, "timestamp": datetime.fromisoformat(ts.replace("Z", "+00:00"))}
        return cls(**payload)


def runtime_dir(tesseract_home: Path) -> Path:
    """Resolve ``<TESSERACT_HOME>/runtime/`` and create on demand.

    All supervisor-owned state files (intent.json, crash_storm.json,
    supervisor.pid) live here. Gitignored under TESSERACT_HOME.
    """
    target = tesseract_home / "runtime"
    target.mkdir(parents=True, exist_ok=True)
    return target


def intent_path(tesseract_home: Path) -> Path:
    return runtime_dir(tesseract_home) / "intent.json"


def write_atomic(path: Path, intent: IntentFile) -> None:
    """Tmp-file + rename so a partially-flushed write is never visible.

    A stale intent file is worse than no file (it could mis-route a
    subsequent crash as operator_quit); callers must use a fresh
    ``timestamp`` and ``backend_pid`` so the reader's staleness check
    can throw it out if the producing process is gone.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = intent.to_payload()
    # NamedTemporaryFile on Windows can't be reopened by another handle,
    # so we mkstemp + manual write/rename instead.
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".intent-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        # Best-effort cleanup of the tmp; the rename happens-or-not.
        try:
            Path(tmp_name).unlink()
        except OSError:
            pass
        raise


def read_with_staleness_check(
    path: Path,
    *,
    backend_started_at: datetime,
    backend_pid: int | None = None,
) -> IntentFile | None:
    """Read the intent file, treating these as absent:

    * file does not exist;
    * file is malformed JSON or fails IntentFile validation;
    * intent timestamp is older than ``backend_started_at`` (stale —
      written by a prior backend run);
    * file's ``backend_pid`` is set and disagrees with ``backend_pid``
      (also stale — different backend instance wrote it).

    Returns ``None`` on any of those, which the supervisor treats as
    ``crash``. Returns the parsed ``IntentFile`` otherwise.
    """
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        intent = IntentFile.from_payload(payload)
    except (json.JSONDecodeError, ValueError, KeyError, TypeError):
        return None
    if intent.timestamp < backend_started_at:
        return None
    if (
        backend_pid is not None
        and intent.backend_pid is not None
        and intent.backend_pid != backend_pid
    ):
        return None
    return intent


def clear_intent(path: Path) -> None:
    """Remove the intent file. Called after the supervisor has routed on
    it so the next backend run starts with a clean slate."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "IntentFile",
    "IntentKind",
    "IntentSource",
    "intent_path",
    "runtime_dir",
    "write_atomic",
    "read_with_staleness_check",
    "clear_intent",
    "now_utc",
]
