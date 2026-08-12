"""Controller session registry + Mirror chat record primitives.

Two distinct record kinds share this module because both are JSON-on-disk
and both must be readable / writable from the controller daemon, the agent
TUI, and the Mirror backend:

* `ControllerSessionRecord` — durable identity for one controller
  session. Lives at
  `<TESSERACT_HOME>/agent_controller/sessions/<session_id>.json`.

* `ChatRecord` — Mirror multi-chat persistent record. Lives at
  `<TESSERACT_HOME>/sessions/chats/<uuid>.json`. The two namespaces
  never collide (session ids are dated+hex, chats are bare UUID4).

TC-2 ships read/write primitives only — no Mirror UI changes. The shape
is the contract multi-chat consumes.
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field

from tesseract.orchestrator.activity.hooks import (
    register_session,
    update_session_state,
)

from .lanes.principals import OPERATOR_PRINCIPAL
from .paths import (
    chats_dir,
    mint_session_id,
    session_record_path,
    sessions_dir,
    transcript_path,
)

SessionStatus = Literal["active", "idle", "detached", "closed"]
SessionMode = Literal["chat", "autonomy", "scheduler"]
SessionOrigin = Literal["cli", "mirror", "autonomy", "scheduler", "telegram"]


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class ControllerSessionRecord(BaseModel):
    """On-disk shape per `_shared/session-registry-schema.md`."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    title: str | None = None
    mode: SessionMode
    origin: SessionOrigin
    created_at: str = Field(default_factory=_now_iso)
    last_active_at: str = Field(default_factory=_now_iso)
    status: SessionStatus = "active"
    controller_id: str | None = None
    transcript_path: str
    chat_id: str | None = None
    child_worker_ids: list[str] = Field(default_factory=list)
    pending_approval_ids: list[str] = Field(default_factory=list)
    preferred_seat: str | None = None
    # The MCP client identity that asked for this session (`agent.assign`), or
    # "operator" for anything the runtime started itself. `agent.status` and
    # `agent.review` authorize against it; a record written before this field
    # existed loads as the operator's rather than as nobody's.
    owner_principal: str = OPERATOR_PRINCIPAL


class ChatRecord(BaseModel):
    """Mirror multi-chat persistent record shape."""

    model_config = ConfigDict(extra="allow")

    chat_id: str
    title: str | None = None
    created_at: str = Field(default_factory=_now_iso)
    last_message_at: str = Field(default_factory=_now_iso)
    model_role: str = "chat_brain"
    session_id: str | None = None
    message_count: int = 0


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON via tmp+rename so a crash mid-write can't truncate the record.

    The tmp filename carries a random hex suffix so concurrent writers
    targeting the same record (reviewer I-1, 2026-05-23) do not race on a
    shared ``<path>.tmp`` file before the atomic ``os.replace``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{secrets.token_hex(4)}.tmp")
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


class SessionRegistry:
    """CRUD primitives for `ControllerSessionRecord` + `ChatRecord`.

    All path lookups go through `paths.py` so a test that monkeypatches
    `TESSERACT_HOME` after import still routes writes to tmp_path.
    """

    # ── controller sessions ─────────────────────────────────────────────

    def create_session(
        self,
        *,
        mode: SessionMode,
        origin: SessionOrigin,
        controller_id: str | None = None,
        title: str | None = None,
        chat_id: str | None = None,
        session_id: str | None = None,
        preferred_seat: str | None = None,
        owner_principal: str = OPERATOR_PRINCIPAL,
    ) -> ControllerSessionRecord:
        sid = session_id or mint_session_id()
        record = ControllerSessionRecord(
            session_id=sid,
            title=title,
            mode=mode,
            origin=origin,
            controller_id=controller_id,
            transcript_path=str(transcript_path(sid)),
            chat_id=chat_id,
            preferred_seat=preferred_seat,
            owner_principal=owner_principal,
        )
        self._write_session(record)
        # AS-1 — project the controller session into the activity registry.
        register_session(
            record.session_id,
            label=record.title or record.mode,
            status=record.status,
            owner_principal=owner_principal,
        )
        return record

    def get_session(self, session_id: str) -> ControllerSessionRecord | None:
        path = session_record_path(session_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return ControllerSessionRecord.model_validate(payload)

    def update_session(
        self,
        session_id: str,
        *,
        status: SessionStatus | None = None,
        title: str | None = None,
        chat_id: str | None = None,
        child_worker_ids: list[str] | None = None,
        pending_approval_ids: list[str] | None = None,
        touch_last_active: bool = True,
    ) -> ControllerSessionRecord:
        record = self.get_session(session_id)
        if record is None:
            raise KeyError(f"unknown controller session: {session_id}")
        updates: dict = {}
        if status is not None:
            updates["status"] = status
        if title is not None:
            updates["title"] = title
        if chat_id is not None:
            updates["chat_id"] = chat_id
        if child_worker_ids is not None:
            updates["child_worker_ids"] = list(child_worker_ids)
        if pending_approval_ids is not None:
            updates["pending_approval_ids"] = list(pending_approval_ids)
        if touch_last_active:
            updates["last_active_at"] = _now_iso()
        if not updates:
            return record
        updated = record.model_copy(update=updates)
        self._write_session(updated)
        # AS-1 — reflect a status transition (active/idle/detached/closed).
        if status is not None:
            update_session_state(session_id, status)
        return updated

    def list_sessions(
        self,
        *,
        status: SessionStatus | None = None,
    ) -> list[ControllerSessionRecord]:
        root = sessions_dir()
        if not root.exists():
            return []
        records: list[ControllerSessionRecord] = []
        for path in sorted(root.glob("*.json")):
            try:
                with path.open("r", encoding="utf-8") as fh:
                    payload = json.load(fh)
                record = ControllerSessionRecord.model_validate(payload)
            except (json.JSONDecodeError, ValueError):
                continue
            if status is not None and record.status != status:
                continue
            records.append(record)
        return records

    def delete_session(self, session_id: str) -> bool:
        """Remove the session record + transcript file. Idempotent.

        Returns ``True`` if a record existed and was removed,
        ``False`` if no record was present (treated as a successful
        no-op so concurrent deletes don't surface as errors). The
        transcript is unlinked best-effort — a missing transcript
        file is not an error since transcripts are append-only and
        may simply have never been touched.
        """
        record_path = session_record_path(session_id)
        existed = record_path.exists()
        try:
            record_path.unlink()
        except FileNotFoundError:
            pass
        try:
            transcript_path(session_id).unlink()
        except FileNotFoundError:
            pass
        return existed

    def _write_session(self, record: ControllerSessionRecord) -> None:
        path = session_record_path(record.session_id)
        _atomic_write_json(path, record.model_dump(mode="json"))

    # ── chats (Mirror multi-chat) ───────────────────────────────────────

    def create_chat(
        self,
        *,
        title: str | None = None,
        session_id: str | None = None,
        chat_id: str | None = None,
        model_role: str = "chat_brain",
    ) -> ChatRecord:
        cid = chat_id or str(uuid.uuid4())
        record = ChatRecord(
            chat_id=cid,
            title=title,
            session_id=session_id,
            model_role=model_role,
        )
        self._write_chat(record)
        return record

    def get_chat(self, chat_id: str) -> ChatRecord | None:
        path = chats_dir() / f"{chat_id}.json"
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return ChatRecord.model_validate(payload)

    def update_chat(
        self,
        chat_id: str,
        *,
        title: str | None = None,
        session_id: str | None = None,
        message_count_delta: int = 0,
        touch_last_message: bool = True,
    ) -> ChatRecord:
        record = self.get_chat(chat_id)
        if record is None:
            raise KeyError(f"unknown chat: {chat_id}")
        updates: dict = {}
        if title is not None:
            updates["title"] = title
        if session_id is not None:
            updates["session_id"] = session_id
        if message_count_delta:
            updates["message_count"] = record.message_count + message_count_delta
        if touch_last_message:
            updates["last_message_at"] = _now_iso()
        if not updates:
            return record
        updated = record.model_copy(update=updates)
        self._write_chat(updated)
        return updated

    def list_chats(self) -> list[ChatRecord]:
        root = chats_dir()
        if not root.exists():
            return []
        records: list[ChatRecord] = []
        for path in sorted(root.glob("*.json")):
            try:
                with path.open("r", encoding="utf-8") as fh:
                    payload = json.load(fh)
                records.append(ChatRecord.model_validate(payload))
            except (json.JSONDecodeError, ValueError):
                continue
        return records

    def _write_chat(self, record: ChatRecord) -> None:
        path = chats_dir() / f"{record.chat_id}.json"
        _atomic_write_json(path, record.model_dump(mode="json"))


def iter_session_paths() -> Iterator[Path]:
    root = sessions_dir()
    if not root.exists():
        return
    yield from sorted(root.glob("*.json"))


__all__ = [
    "ChatRecord",
    "ControllerSessionRecord",
    "SessionMode",
    "SessionOrigin",
    "SessionRegistry",
    "SessionStatus",
    "iter_session_paths",
]
