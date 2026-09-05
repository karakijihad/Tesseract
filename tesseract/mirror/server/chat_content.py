"""Message content — what a conversation file holds, independent of who owns it.

Three functions with no notion of a chat, a session or an identity: make a
history safe to write, pull readable text out of one message, and hand a saved
conversation to the work index.
"""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def sanitize_history_for_persistence(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a save-safe history copy.

    Two things never reach disk.

    Attachment bytes: a turn may carry base64 file data while an adapter
    request is in flight. The record keeps the metadata; the raw files already
    live under `TESSERACT_HOME/uploads/chat`.

    Reasoning items (`_reasoning`): the model's own scratchpad, and the only
    provider-shaped state in the history — an `encrypted_content` blob on the
    OpenAI Responses path, a thinking block on Anthropic, nothing at all on
    Gemini. The adapters need it within a live turn, where it stays in memory.
    Persisting it buys a restored conversation slightly warmer context and
    costs a blob with an opaque server-side TTL that errors the resume once it
    expires, plus a second failure mode where a compaction drops the item a
    reasoning entry points at. Operator ruling 2026-08-19: it is dropped at the
    persistence boundary.

    Applied on read as well as on write, so a record written before that ruling
    is cleaned the first time it is loaded.

    A third thing, and it is a BACKSTOP rather than the defence: a stored
    credential. The wall is on the way in, at `brain/tools.py::execute_tool`,
    so a value should never be in a history that reaches here. This catches one
    that arrived by some route that wall does not cover, and it runs on read as
    well as write, so a session file written before the wall existed is cleaned
    the first time it is loaded.

    It DEGRADES rather than failing closed. A credential store that cannot be
    read must not stop a conversation being saved — losing the record is worse
    than the risk it was checking for, and the path that fails closed is the
    provider request, where the value would leave the machine.
    """
    sanitized: list[dict[str, Any]] = []
    for msg in history:
        if msg.get("_reasoning"):
            continue
        msg = copy.deepcopy(msg)
        content = msg.get("content")
        if isinstance(content, list):
            msg["content"] = [
                {k: v for k, v in part.items() if k != "data"}
                for part in content
                if isinstance(part, dict)
            ]
        sanitized.append(msg)
    try:
        from tesseract.credentials.redaction import redact_payload

        return redact_payload(sanitized)
    except Exception as exc:  # noqa: BLE001 — a save must not fail on this
        logger.error(
            "history could not be checked for credentials before persistence: %s", exc
        )
        return sanitized


def extract_message_text(content: Any) -> str:
    """OpenAI/Gemini histories use mixed content shapes — stringify just
    enough to render a preview. Tool-call blocks and image parts are
    skipped to keep the popover readable.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                t = item.get("type")
                if t in ("text", "input_text", "output_text"):
                    val = item.get("text") or item.get("content") or ""
                    if isinstance(val, str):
                        parts.append(val)
        return " ".join(parts).strip()
    return ""


def last_message_timestamp(history: list[dict[str, Any]]) -> str | None:
    """Timestamp of the most recent message actually appended to a history.

    Walks in reverse for the first entry carrying a ``timestamp`` field
    (stamped per-message in ``brain/chat.py``, and it survives persistence).
    Entries without one are skipped; ``None`` when nothing is stamped — an
    empty conversation, or history predating the field.

    Here rather than beside either caller: the chat store asks it to decide
    when a conversation was last active, and the legacy migration asks it to
    date a file whose header lies. Two copies of the rule would drift.
    """
    for msg in reversed(history):
        ts = msg.get("timestamp")
        if isinstance(ts, str) and ts:
            return ts
    return None


def index_conversation_file(path: Path) -> None:
    """Index a freshly-saved conversation JSON into the work index.

    `index_session_file` keys recall chunks by the file stem — the chat_id —
    so each conversation is a separately-tagged recall source.

    Best-effort: any failure is swallowed (the save already succeeded).
    Resolves the DB path via the canonical ``env-or-import-constant``
    pattern — env override wins (test fixtures using ``monkeypatch.setenv``
    get isolated indexes), default falls back to
    ``tesseract.paths.TESSERACT_HOME`` so dev / default-home runtimes
    index instead of silently skipping. Matches `tesseract/kernel/
    workspace_changes.py::workspace_events_dir`.
    """
    try:
        from tesseract.memory.work_index import WorkIndex
        from tesseract.memory.work_ingester import index_session_file
        from tesseract.paths import TESSERACT_HOME as _DEFAULT_HOME
    except Exception:  # noqa: BLE001
        return
    home = Path(os.environ.get("TESSERACT_HOME") or _DEFAULT_HOME)
    db_path = home / "work_index.sqlite"
    try:
        idx = WorkIndex(db_path)
    except Exception:  # noqa: BLE001
        return
    try:
        index_session_file(idx, path)
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            idx.close()
        except Exception:  # noqa: BLE001
            pass
