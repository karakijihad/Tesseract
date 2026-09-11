"""The chat record — one conversation, one file, one identity.

``<TESSERACT_HOME>/sessions/chats/<chat_id>.json``. The file is canonical; the
live ``ServerSession.chats`` registry is the in-memory view a connected surface
holds of it.

Format (schema 1)::

    {
      "schema": 1, "chat_id", "session_id", "title",
      "created_at", "started_at", "ended_at",
      "archived", "turn_count", "model", "history": [...]
    }

``chat_id`` is a uuid4 hex (32 lowercase hex chars), stamped once at creation
and never derived from a clock, a connection or a filename — validated on every
path so a crafted id cannot escape the chats directory. ``session_id`` is the
per-connection uuid that last wrote the record; it is provenance for spawn
liveness (``ChatSession.mark_vanished_spawns``), never identity.

This module owns the file and nothing else: the shape, the directory, and the
raw read and write. Who saves, when, and what else a save updates is
``chat_store``'s.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from tesseract.lib.yaml_io import atomic_write_text
from tesseract.mirror.server.chat_content import sanitize_history_for_persistence
from tesseract.paths import home_dir

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_CHAT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def is_valid_chat_id(chat_id: str) -> bool:
    return bool(_CHAT_ID_RE.fullmatch(chat_id or ""))


def chats_dir() -> Path:
    """Return ``<TESSERACT_HOME>/sessions/chats``, resolving env at call time.

    ``paths.home_dir()`` rather than a local copy of the env-or-default rule:
    that module exists because a hand-rolled copy in a deeper module never
    honours the env var, and three of them lived in this file.
    """
    return home_dir() / "sessions" / "chats"


def chat_path(chat_id: str) -> Path:
    return chats_dir() / f"{chat_id}.json"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


#: The wall-clock stamp a chat wears until the operator renames it. Defined
#: here rather than at the one call site because two readers need it: the
#: creator, and `chat_store` asking whether a title is still the default one.
DEFAULT_TITLE_FORMAT = "%Y-%m-%d %H:%M"


def default_chat_title(created: datetime | str) -> str:
    """The title a chat is born with, from its creation stamp.

    Accepts the datetime the creator holds or the ISO string a record
    carries, so "is this still unnamed?" is answered the same way on both
    sides. An unparseable stamp yields "" and matches no title, so a record
    with a broken timestamp is treated as named and kept.
    """
    if isinstance(created, str):
        try:
            created = datetime.fromisoformat(created)
        except ValueError:
            return ""
    return created.strftime(DEFAULT_TITLE_FORMAT)


@dataclass
class ChatRecord:
    chat_id: str
    session_id: str
    title: str
    created_at: str
    started_at: str
    history: list[dict[str, Any]] = field(default_factory=list)
    archived: bool = False
    turn_count: int = 0
    ended_at: str | None = None
    model: str = ""
    # Which door the conversation came through. A channel chat is a chat —
    # same store, same restore, same recall — and this is what lets a reader
    # tell them apart WITHOUT a second store. Defaulting to "cockpit" keeps
    # every record written before this field correct rather than unknown.
    surface: str = "cockpit"
    # WHOSE conversation this is, as `<channel>:<chat id>`. Empty on a cockpit
    # record, which is the operator's own by definition because they are the
    # one sitting at it.
    #
    # **Empty on a CHANNEL record means nobody**, not the operator, and the
    # two are read that way round deliberately: such a record was written
    # before this field existed, and `chat_store.belongs_to_operator` refuses
    # it rather than admitting it. Reading an unknown as the operator's is the
    # failure the whole split exists to stop, so the predicate is the thing to
    # believe here and this comment used to say the opposite.
    #
    # `surface` says how a conversation arrived and cannot answer this. On a
    # machine with a second approved channel user, both of their conversations
    # are `surface="channel"` and only one of them belongs in the operator's
    # own digest, library and recall.
    #
    # It has to be STORED rather than derived: a live channel record is keyed
    # on `durable_chat_id`, which names the chat, but `archive_copy` mints a
    # fresh uuid and the archived copy is exactly what the daily jobs read.
    principal: str = ""
    schema: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "chat_id": self.chat_id,
            "session_id": self.session_id,
            "title": self.title,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "archived": self.archived,
            "turn_count": self.turn_count,
            "model": self.model,
            "surface": self.surface,
            "principal": self.principal,
            "history": self.history,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChatRecord:
        return cls(
            schema=data.get("schema", SCHEMA_VERSION),
            chat_id=data["chat_id"],
            session_id=data.get("session_id", ""),
            title=data.get("title", ""),
            created_at=data.get("created_at", ""),
            started_at=data.get("started_at", ""),
            ended_at=data.get("ended_at"),
            archived=bool(data.get("archived", False)),
            turn_count=int(data.get("turn_count", 0)),
            model=str(data.get("model") or ""),
            surface=str(data.get("surface") or "cockpit"),
            principal=str(data.get("principal") or ""),
            history=data.get("history", []),
        )


def iter_history_files() -> Iterator[Path]:
    """Yield every chat record file, in stem order.

    The stem is a uuid4, so that order carries no chronology — a caller that
    wants newest-first sorts the records it loads, it does not read the name.

    For consumers that want the files rather than parsed records — the
    work-index backfill is the one — so the directory keeps a single owner
    instead of growing a walk per caller.
    """
    directory = chats_dir()
    if not directory.exists():
        return
    yield from sorted(directory.glob("*.json"))


def read_record(chat_id: str) -> ChatRecord | None:
    """Parse one record off disk, or None if missing / unreadable / invalid id.

    History is sanitized on the way in as well as on the way out, so a record
    written before reasoning items stopped being persisted is cleaned the first
    time anything reads it.
    """
    if not is_valid_chat_id(chat_id):
        return None
    path = chat_path(chat_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        record = ChatRecord.from_dict(data)
    except Exception as exc:  # noqa: BLE001 — corrupt file shouldn't crash a list
        logger.warning("chat load failed (%s): %s", path, exc)
        return None
    record.history = sanitize_history_for_persistence(record.history)
    return record


def write_record(record: ChatRecord) -> Path:
    """Write one record to its file. Raises ``ValueError`` on a malformed id.

    History is sanitized here rather than only in ``chat_store.save_chat``:
    this is the one seam that writes the file, so a second writer — the legacy
    migration is one — cannot persist a reasoning item or a base64 attachment
    by forgetting to. The filter is idempotent, so a caller that sanitized
    already pays a copy and nothing else.

    Atomic, not ``write_text``: autosave rewrites this file on a timer, so a
    kill or power cut during a write is the exact event it exists to survive —
    and a truncated file is worse than a stale one, because ``read_record``
    discards malformed JSON and the chat is then simply gone. Temp-then-replace
    keeps the previous good copy until the new one is complete.
    """
    if not is_valid_chat_id(record.chat_id):
        raise ValueError(f"invalid chat_id: {record.chat_id!r}")
    directory = chats_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = chat_path(record.chat_id)
    payload = record.to_dict()
    payload["history"] = sanitize_history_for_persistence(record.history)
    atomic_write_text(path, json.dumps(payload, indent=2))
    return path
