"""SessionRegistry CRUD + Mirror chat record primitives."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from tesseract.orchestrator.tars_controller import (
    ChatRecord,
    ControllerSessionRecord,
    SessionRegistry,
    chats_dir,
    mint_session_id,
    sessions_dir,
)


# ── controller sessions ─────────────────────────────────────────────────────


def test_create_session_writes_valid_json(isolated_home: Path) -> None:
    reg = SessionRegistry()
    record = reg.create_session(mode="chat", origin="cli")
    path = sessions_dir() / f"{record.session_id}.json"
    assert path.exists()
    payload = json.loads(path.read_text("utf-8"))
    assert payload["session_id"] == record.session_id
    assert payload["mode"] == "chat"
    assert payload["origin"] == "cli"
    assert payload["status"] == "active"
    assert payload["transcript_path"].endswith(f"{record.session_id}.jsonl")
    assert payload["child_worker_ids"] == []
    assert payload["pending_approval_ids"] == []


def test_get_session_round_trips(isolated_home: Path) -> None:
    reg = SessionRegistry()
    created = reg.create_session(
        mode="autonomy", origin="autonomy", title="test session",
    )
    loaded = reg.get_session(created.session_id)
    assert loaded is not None
    assert loaded.session_id == created.session_id
    assert loaded.title == "test session"
    assert loaded.mode == "autonomy"
    assert loaded.origin == "autonomy"


def test_get_session_returns_none_when_missing(isolated_home: Path) -> None:
    reg = SessionRegistry()
    assert reg.get_session("2026-05-23-deadbeef") is None


def test_update_session_changes_status_and_worker_ids(isolated_home: Path) -> None:
    reg = SessionRegistry()
    created = reg.create_session(mode="chat", origin="cli")
    updated = reg.update_session(
        created.session_id,
        status="detached",
        child_worker_ids=["w-1", "w-2"],
        pending_approval_ids=["a-1"],
    )
    assert updated.status == "detached"
    assert updated.child_worker_ids == ["w-1", "w-2"]
    assert updated.pending_approval_ids == ["a-1"]
    # last_active_at should advance
    assert updated.last_active_at >= created.last_active_at

    reloaded = reg.get_session(created.session_id)
    assert reloaded is not None
    assert reloaded.status == "detached"
    assert reloaded.child_worker_ids == ["w-1", "w-2"]


def test_update_session_unknown_id_raises(isolated_home: Path) -> None:
    reg = SessionRegistry()
    with pytest.raises(KeyError):
        reg.update_session("2026-05-23-deadbeef", status="closed")


def test_list_sessions_filters_by_status(isolated_home: Path) -> None:
    reg = SessionRegistry()
    s1 = reg.create_session(mode="chat", origin="cli")
    s2 = reg.create_session(mode="chat", origin="cli")
    reg.update_session(s2.session_id, status="closed")
    active_ids = {r.session_id for r in reg.list_sessions(status="active")}
    closed_ids = {r.session_id for r in reg.list_sessions(status="closed")}
    assert s1.session_id in active_ids
    assert s2.session_id in closed_ids
    assert s2.session_id not in active_ids
    assert s1.session_id not in closed_ids


def test_list_sessions_skips_corrupt_files(isolated_home: Path) -> None:
    reg = SessionRegistry()
    good = reg.create_session(mode="chat", origin="cli")
    bad_path = sessions_dir() / "2026-05-23-baadbaad.json"
    bad_path.write_text("{not json", encoding="utf-8")
    records = reg.list_sessions()
    ids = {r.session_id for r in records}
    assert good.session_id in ids
    assert "2026-05-23-baadbaad" not in ids


def test_session_id_can_be_supplied(isolated_home: Path) -> None:
    reg = SessionRegistry()
    sid = mint_session_id()
    record = reg.create_session(mode="chat", origin="cli", session_id=sid)
    assert record.session_id == sid


def test_create_session_invalid_supplied_id_raises(isolated_home: Path) -> None:
    reg = SessionRegistry()
    with pytest.raises(ValueError):
        reg.create_session(mode="chat", origin="cli", session_id="../escape")


# ── chats (Mirror multi-chat) ──────────────────────────────────────────────


def test_create_chat_writes_uuid_record(isolated_home: Path) -> None:
    reg = SessionRegistry()
    chat = reg.create_chat(title="design")
    # Default chat_id is a UUID4
    uuid.UUID(chat.chat_id)
    path = chats_dir() / f"{chat.chat_id}.json"
    assert path.exists()
    payload = json.loads(path.read_text("utf-8"))
    # Spec-required fields
    for key in (
        "chat_id",
        "title",
        "created_at",
        "last_message_at",
        "model_role",
        "session_id",
        "message_count",
    ):
        assert key in payload
    assert payload["model_role"] == "chat_brain"
    assert payload["message_count"] == 0
    assert payload["session_id"] is None


def test_update_chat_increments_message_count(isolated_home: Path) -> None:
    reg = SessionRegistry()
    chat = reg.create_chat()
    updated = reg.update_chat(chat.chat_id, message_count_delta=3)
    assert updated.message_count == 3
    updated2 = reg.update_chat(chat.chat_id, message_count_delta=2)
    assert updated2.message_count == 5


def test_chat_links_to_session_via_session_id(isolated_home: Path) -> None:
    reg = SessionRegistry()
    sess = reg.create_session(mode="chat", origin="mirror")
    chat = reg.create_chat(session_id=sess.session_id)
    assert chat.session_id == sess.session_id
    loaded = reg.get_chat(chat.chat_id)
    assert loaded is not None
    assert loaded.session_id == sess.session_id


def test_list_chats(isolated_home: Path) -> None:
    reg = SessionRegistry()
    c1 = reg.create_chat()
    c2 = reg.create_chat()
    ids = {c.chat_id for c in reg.list_chats()}
    assert ids == {c1.chat_id, c2.chat_id}
