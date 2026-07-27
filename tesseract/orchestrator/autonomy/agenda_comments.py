"""AgendaCommentLog — append-only per-item discussion threads.

One JSONL file per agenda item at ``<TESSERACT_HOME>/agenda/comments/
<item_id>.jsonl``. Append-only — every line is a single comment with
``{at, by, role, body}``. ``role`` is ``operator`` or ``agent`` so the
UI can lane the messages; ``by`` is the session id (operator) or the
agent slug. Body is plain text, capped at 4000 chars.

Reasoning: operator-facing approval items need a place for the operator
to ask "what does this actually entail?" and (later) for an agent to
answer back, without bolting onto ``workspace_events`` (memory: that
substrate is reserved for operator-attended threads from the agent
side). The comment file is gitignored along with the rest of
``tesseract/agenda/``.

Reads return the full thread; the dashboard shows the last N. There
is no edit / delete primitive — the audit value of an append-only log
beats UX convenience here.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from tesseract.orchestrator.autonomy.paths import (
    agenda_comments_dir,
    agenda_comments_path,
)

log = logging.getLogger(__name__)


MAX_BODY_CHARS = 4000


CommentRole = Literal["operator", "agent"]


class AgendaComment(BaseModel):
    """One entry in an item's thread.

    ``id`` is a 12-hex random suffix appended at write time so the UI can
    key React lists without depending on identical timestamps. ``at`` is
    UTC ISO; ``role`` lanes who said it; ``by`` is the session id
    (operator) or agent slug; ``body`` is plain text.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    at: datetime
    role: CommentRole
    by: str
    body: str = Field(max_length=MAX_BODY_CHARS)


def _mint_comment_id() -> str:
    return f"cm-{secrets.token_hex(6)}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def append_comment(
    item_id: str,
    *,
    role: CommentRole,
    by: str,
    body: str,
    now: datetime | None = None,
) -> AgendaComment:
    """Append a single comment to the item's thread. Creates the dir +
    file on first write. Atomic per line: a single ``write`` syscall on
    POSIX is atomic for sub-PIPE_BUF writes; on Windows the same holds
    for the default buffer. The file is opened with ``O_APPEND`` so
    concurrent appenders don't overwrite each other."""
    body = (body or "").strip()
    if not body:
        raise ValueError("comment body must not be empty")
    if len(body) > MAX_BODY_CHARS:
        raise ValueError(
            f"comment body exceeds {MAX_BODY_CHARS} chars (got {len(body)})"
        )
    if role not in ("operator", "agent"):
        raise ValueError(f"invalid comment role: {role!r}")

    comment = AgendaComment(
        id=_mint_comment_id(),
        at=now or _utcnow(),
        role=role,
        by=by,
        body=body,
    )
    path = agenda_comments_path(item_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(comment.model_dump(mode="json"), ensure_ascii=False) + "\n"
    # O_APPEND keeps multi-process appenders from overlapping; encoding
    # explicit so a future Windows default flip doesn't silently switch.
    with open(path, "a", encoding="utf-8", newline="") as fp:
        fp.write(line)
    return comment


def list_comments(item_id: str) -> list[AgendaComment]:
    """Return every comment for the item in chronological order.

    Missing file → empty list (a never-commented item is the common
    case). Corrupt lines are skipped + logged; one bad line must not
    blank the thread. Caller wanting the most recent N can slice the
    tail."""
    path = agenda_comments_path(item_id)
    if not path.exists():
        return []
    out: list[AgendaComment] = []
    with open(path, "r", encoding="utf-8") as fp:
        for line_no, raw in enumerate(fp, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
                out.append(AgendaComment.model_validate(payload))
            except (json.JSONDecodeError, ValueError) as exc:
                log.warning(
                    "agenda_comments: skipping corrupt line %d in %s: %s",
                    line_no,
                    path.name,
                    exc,
                )
                continue
    return out


__all__ = [
    "AgendaComment",
    "CommentRole",
    "MAX_BODY_CHARS",
    "append_comment",
    "list_comments",
]
