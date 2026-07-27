"""Call-time path resolvers for the controller substrate.

All resolvers honor `TESSERACT_HOME` env overrides applied AFTER import,
matching the `kernel/workspace_changes.py::workspace_events_dir` pattern
so monkeypatching `TESSERACT_HOME` in tests routes writes to tmp_path
rather than the production tree.
"""

from __future__ import annotations

import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from tesseract.paths import TESSERACT_HOME

SESSION_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[0-9a-f]{8}$")


def _home() -> Path:
    override = os.environ.get("TESSERACT_HOME")
    return Path(override).resolve() if override else TESSERACT_HOME


def controller_dir() -> Path:
    return _home() / "tars_controller"


def sessions_dir() -> Path:
    return controller_dir() / "sessions"


def transcripts_dir() -> Path:
    return controller_dir() / "transcripts"


def chats_dir() -> Path:
    return _home() / "sessions" / "chats"


def controller_record_path() -> Path:
    return controller_dir() / "controller.json"


def run_dir() -> Path:
    return _home() / "run"


def port_file_path() -> Path:
    return run_dir() / "controller.port"


def token_file_path() -> Path:
    return run_dir() / "controller.token"


def heartbeat_path(controller_id: str) -> Path:
    return controller_dir() / controller_id / "heartbeat"


def _validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        raise ValueError(f"invalid controller session id: {session_id!r}")


def session_record_path(session_id: str) -> Path:
    _validate_session_id(session_id)
    return sessions_dir() / f"{session_id}.json"


def transcript_path(session_id: str) -> Path:
    _validate_session_id(session_id)
    return transcripts_dir() / f"{session_id}.jsonl"


def mint_session_id(*, today: str | None = None) -> str:
    """`<YYYY-MM-DD>-<8 hex chars>` per the registry-schema contract."""
    date_part = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    hex_part = secrets.token_hex(4)
    return f"{date_part}-{hex_part}"


__all__ = [
    "SESSION_ID_RE",
    "chats_dir",
    "controller_dir",
    "controller_record_path",
    "heartbeat_path",
    "mint_session_id",
    "port_file_path",
    "run_dir",
    "session_record_path",
    "sessions_dir",
    "token_file_path",
    "transcript_path",
    "transcripts_dir",
]
