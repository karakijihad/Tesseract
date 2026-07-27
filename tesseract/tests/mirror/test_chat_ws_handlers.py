"""mirror-multi-chat P1 inc.3b — chat-lifecycle WS handlers.

Offline unit tests for `_handle_chat_create/switch/archive`: switch + archive
run end-to-end on a real ServerSession (fake ws + fake ChatSessions); create
uses a monkeypatched `new_chat_session` seam, since the real ChatSession build
needs boot infra (`app["adapter_entry"]`) and is exercised server-up.

NOTE: the live WS round-trip (browser → backend) is NOT verified here — the
backend was down when these landed. Handler orchestration + envelopes are
unit-verified; end-to-end smoke is pending server-up.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from tesseract.mirror.server import chat_lifecycle as chat_lifecycle_mod
from tesseract.mirror.server import chat_store, ws as ws_mod
from tesseract.mirror.server import session as session_mod
from tesseract.mirror.server import session_factory as session_factory_mod
from tesseract.mirror.server.chat_store import ChatRecord
from tesseract.mirror.server.session import ChatInfraNotReady, ServerSession


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


def _iso(offset_days: int = 0, *, hour: int = 10) -> str:
    """Wall-clock-relative ISO stamp (today +/- N days) — day-rollover tests
    must not hardcode absolute dates (they'd go stale and start failing once
    `archive_stale_open_chats` treats them as prior-day)."""
    now = datetime.now().astimezone().replace(hour=hour, minute=0, second=0, microsecond=0)
    return (now + timedelta(days=offset_days)).isoformat()


def _save_with_ended_at(record: ChatRecord, ended_at: str) -> None:
    """`save_chat` always re-stamps `ended_at` to real-now — day-rollover
    tests need to pin "last activity" to a specific day instead, so save
    then overwrite the on-disk `ended_at` directly."""
    chat_store.save_chat(record)
    path = chat_store.chats_dir() / f"{record.chat_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["ended_at"] = ended_at
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _session() -> tuple[ServerSession, list[dict]]:
    sent: list[dict] = []

    class _WS:
        closed = False

        async def send_json(self, env: dict) -> None:
            sent.append(env)

    s = ServerSession(
        session_id="abcabcabcabcabcabcabcabcabcabc12",
        ws=_WS(),
        chat_session=SimpleNamespace(history=[{"role": "user", "content": "seed"}]),
        event_log=SimpleNamespace(append=lambda _e: None),
    )
    return s, sent


def _last(sent: list[dict]) -> dict:
    return sent[-1]


def test_restore_rehydrates_open_chats_from_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    """P3 reload — on connect, the session registry is rebuilt from the persisted
    non-archived chats (newest active), so a page reload restores the tabs.
    Archived chats are excluded; each restored chat carries its history.
    All timestamps are today's — same-day reconnect, so day-rollover
    archiving must not touch these."""
    s, _ = _session()
    # `_restore_persisted_chats` (chat_restore.py) resolves `new_chat_session`
    # from session_factory.py at call time — patching the session.py barrel
    # here would be a dead patch (session.py's copy is never re-read).
    monkeypatch.setattr(
        session_factory_mod, "new_chat_session", lambda app, sess, **kw: SimpleNamespace(history=[])
    )
    chat_store.save_chat(ChatRecord(
        chat_id="a" * 32, session_id="x", title="Old",
        created_at=_iso(hour=10), started_at=_iso(hour=10),
    ))
    chat_store.save_chat(ChatRecord(
        chat_id="b" * 32, session_id="x", title="New",
        created_at=_iso(hour=11), started_at=_iso(hour=11),
        history=[{"role": "user", "content": "hello"}],
    ))
    chat_store.save_chat(ChatRecord(
        chat_id="c" * 32, session_id="x", title="Archived", archived=True,
        created_at=_iso(hour=9), started_at=_iso(hour=9),
    ))

    session_mod._restore_persisted_chats(object(), s)

    assert set(s.chats) == {"a" * 32, "b" * 32}  # archived excluded; empty seed replaced
    assert s.chat_order == ["a" * 32, "b" * 32]  # oldest-first insertion order
    assert s.active_chat_id == "b" * 32           # newest is active
    assert s.chat_session is s.chats["b" * 32]
    assert s.chats["b" * 32].history == [{"role": "user", "content": "hello"}]


def test_restore_keeps_fresh_seed_when_no_persisted_chats() -> None:
    """First run (empty library) — restore is a no-op; the single seeded chat
    from __post_init__ stays."""
    s, _ = _session()
    seeded = dict(s.chats)
    session_mod._restore_persisted_chats(object(), s)
    assert s.chats == seeded


def test_restore_archives_stale_chat_and_keeps_fresh_seed_on_new_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Day-rollover (operator request): a connection made after the chat's
    last activity was on a prior local calendar day must NOT resume it —
    the stale chat is auto-archived (still reachable via chat.restore /
    include_archived) and the blank __post_init__ seed is left in place."""
    s, _ = _session()
    # `_restore_persisted_chats` (chat_restore.py) resolves `new_chat_session`
    # from session_factory.py at call time — patching the session.py barrel
    # here would be a dead patch (session.py's copy is never re-read).
    monkeypatch.setattr(
        session_factory_mod, "new_chat_session", lambda app, sess, **kw: SimpleNamespace(history=[])
    )
    _save_with_ended_at(
        ChatRecord(
            chat_id="a" * 32, session_id="x", title="Yesterday",
            created_at=_iso(-1), started_at=_iso(-1),
            history=[{"role": "user", "content": "yesterday's business"}],
        ),
        _iso(-1),
    )
    seeded = dict(s.chats)

    session_mod._restore_persisted_chats(object(), s)

    assert s.chats == seeded  # fresh seed untouched — nothing from today to restore
    assert chat_store.load_chat("a" * 32).archived is True  # reachable, not resumed
    assert chat_store.list_chats(include_archived=True)[0]["chat_id"] == "a" * 32


def test_restore_keeps_todays_chat_and_archives_only_stale_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed library — a chat still active today is restored as usual; a
    chat stranded from a prior day is archived instead of resumed."""
    s, _ = _session()
    # `_restore_persisted_chats` (chat_restore.py) resolves `new_chat_session`
    # from session_factory.py at call time — patching the session.py barrel
    # here would be a dead patch (session.py's copy is never re-read).
    monkeypatch.setattr(
        session_factory_mod, "new_chat_session", lambda app, sess, **kw: SimpleNamespace(history=[])
    )
    _save_with_ended_at(
        ChatRecord(
            chat_id="a" * 32, session_id="x", title="Yesterday",
            created_at=_iso(-1), started_at=_iso(-1),
        ),
        _iso(-1),
    )
    chat_store.save_chat(ChatRecord(
        chat_id="b" * 32, session_id="x", title="Today",
        created_at=_iso(0), started_at=_iso(0),
        history=[{"role": "user", "content": "hi"}],
    ))

    session_mod._restore_persisted_chats(object(), s)

    assert set(s.chats) == {"b" * 32}
    assert s.active_chat_id == "b" * 32
    assert chat_store.load_chat("a" * 32).archived is True


async def test_rename_updates_meta_and_emits() -> None:
    """P3 rename runs over WS (not REST) so the live chat_meta + disk stay in
    lock-step — a REST-only rename would be reverted by the next autosave."""
    s, sent = _session()
    cid = s.active_chat_id
    await ws_mod._handle_chat_rename(object(), s, {"chat_id": cid, "title": "  Vault notes  "})
    assert s.chat_meta[cid].title == "Vault notes"  # trimmed, in-memory updated
    last = _last(sent)
    assert last["type"] == "chat_renamed"
    assert last["data"] == {"chat_id": cid, "title": "Vault notes"}


async def test_rename_rejects_empty_title() -> None:
    s, sent = _session()
    cid = s.active_chat_id
    await ws_mod._handle_chat_rename(object(), s, {"chat_id": cid, "title": "   "})
    last = _last(sent)
    assert last["type"] == "chat_rename_failed"
    assert last["data"]["reason"] == "empty_title"


async def test_rename_rejects_unknown_chat() -> None:
    s, sent = _session()
    await ws_mod._handle_chat_rename(object(), s, {"chat_id": "f" * 32, "title": "x"})
    last = _last(sent)
    assert last["type"] == "chat_rename_failed"
    assert last["data"]["reason"] == "unknown_chat"


def test_open_chats_payload_is_newest_first_with_titles() -> None:
    """P3 reload hydration — session_created carries the open-chat list so the
    tab strip survives a refresh. Newest-first (reverse of insertion-ordered
    chat_order); every entry carries a non-empty title."""
    s, _ = _session()
    c2 = s.create_chat(SimpleNamespace(history=[]))
    c3 = s.create_chat(SimpleNamespace(history=[]))

    payload = ws_mod._open_chats_payload(s)

    ids = [c["chat_id"] for c in payload]
    assert ids[0] == c3 and ids[1] == c2  # newest-first
    assert ids[-1] == s.chat_order[0]      # oldest (seeded) last
    assert all(isinstance(c["title"], str) and c["title"] for c in payload)


async def test_create_registers_and_emits(monkeypatch: pytest.MonkeyPatch) -> None:
    s, sent = _session()
    # `_handle_chat_create` (SDD Task 7.2: moved to chat_lifecycle.py) resolves
    # `new_chat_session` from its own module globals — patching `ws_mod` here
    # would be a dead patch.
    monkeypatch.setattr(chat_lifecycle_mod, "new_chat_session", lambda app, sess, **kw: SimpleNamespace(history=[]))
    # P5 — create now switches active (observer re-wire reads `app`); `{}` = observer off.
    await ws_mod._handle_chat_create({}, s, {})
    assert len(s.chats) == 2
    env = _last(sent)
    assert env["type"] == "chat_created"
    assert env["data"]["chat_id"] in s.chats
    assert env["data"]["title"]


async def test_create_switches_active_to_new_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    """P5 create-desync fix — creating a chat focuses it: the backend active chat
    must move to the new chat so the operator's next `chat_message` (which carries
    no chat_id and runs on `active_chat_id`) lands in the new chat, not the stale
    previously-active one."""
    s, sent = _session()
    old_active = s.active_chat_id
    monkeypatch.setattr(chat_lifecycle_mod, "new_chat_session", lambda app, sess, **kw: SimpleNamespace(history=[]))
    await ws_mod._handle_chat_create({}, s, {})
    new_id = _last(sent)["data"]["chat_id"]
    assert new_id != old_active
    assert s.active_chat_id == new_id
    assert s.chat_session is s.chats[new_id]


async def test_create_infra_not_ready_emits_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    s, sent = _session()

    def _boom(app, sess, **kw):
        raise ChatInfraNotReady("booting")

    monkeypatch.setattr(chat_lifecycle_mod, "new_chat_session", _boom)
    await ws_mod._handle_chat_create(object(), s, {})
    assert len(s.chats) == 1  # no chat added
    assert _last(sent)["type"] == "chat_create_failed"


async def test_create_internal_error_emits_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    s, sent = _session()

    def _boom(app, sess, **kw):
        raise ValueError("config miss in build")

    monkeypatch.setattr(chat_lifecycle_mod, "new_chat_session", _boom)
    # Must NOT propagate (no per-handler guard in _dispatch) — emits a failed env.
    await ws_mod._handle_chat_create(object(), s, {})
    assert len(s.chats) == 1
    assert _last(sent)["type"] == "chat_create_failed"
    assert _last(sent)["data"]["reason"] == "internal_error"


async def test_switch_emits_history_of_target(_home: Path) -> None:
    s, sent = _session()
    cid = s.create_chat(SimpleNamespace(history=[{"role": "user", "content": "other"}]))
    # P4 — switch now consults `app` for observer re-wire; `{}` = observer off (no-op).
    await ws_mod._handle_chat_switch({}, s, {"chat_id": cid})
    assert s.active_chat_id == cid
    env = _last(sent)
    assert env["type"] == "chat_switched"
    assert env["data"]["chat_id"] == cid
    assert env["data"]["history"][0]["content"] == "other"


async def test_switch_history_is_sanitized(_home: Path) -> None:
    s, sent = _session()
    cid = s.create_chat(SimpleNamespace(history=[
        {"role": "user", "content": [{"type": "image", "data": "BIGBYTES", "mime": "image/png"}]},
    ]))
    await ws_mod._handle_chat_switch({}, s, {"chat_id": cid})
    part = _last(sent)["data"]["history"][0]["content"][0]
    assert "data" not in part  # attachment bytes dropped from the switch frame
    assert part["mime"] == "image/png"


async def test_switch_unknown_emits_failed() -> None:
    s, sent = _session()
    await ws_mod._handle_chat_switch(object(), s, {"chat_id": "ffffffffffffffffffffffffffffffff"})
    assert _last(sent)["type"] == "chat_switch_failed"


async def test_archive_non_active_persists_and_emits(_home: Path) -> None:
    s, sent = _session()
    cid = s.create_chat(SimpleNamespace(history=[]))
    await ws_mod._handle_chat_archive(object(), s, {"chat_id": cid})
    env = _last(sent)
    assert env["type"] == "chat_archived"
    assert env["data"]["chat_id"] == cid
    assert cid not in s.chat_order
    # persisted as archived
    assert chat_store.load_chat(cid).archived is True


async def test_archive_last_chat_emits_failed(_home: Path) -> None:
    s, sent = _session()
    await ws_mod._handle_chat_archive(object(), s, {"chat_id": s.active_chat_id})
    env = _last(sent)
    assert env["type"] == "chat_archive_failed"
    assert env["data"]["reason"] == "last_open_chat"


async def test_restore_reopens_in_memory_archived_chat(_home: Path) -> None:
    """P5 — a chat archived THIS session is still in `session.chats`; restore
    un-archives its meta, re-adds it to the open order, and focuses it."""
    s, sent = _session()
    cid = s.create_chat(SimpleNamespace(history=[{"role": "user", "content": "hi"}]))
    await ws_mod._handle_chat_archive({}, s, {"chat_id": cid})
    assert cid not in s.chat_order and cid in s.chats  # in-memory archived

    await ws_mod._handle_chat_restore({}, s, {"chat_id": cid})

    assert cid in s.chat_order
    assert s.active_chat_id == cid
    assert s.chat_meta[cid].archived is False
    env = _last(sent)
    assert env["type"] == "chat_restored"
    assert env["data"]["chat_id"] == cid
    assert chat_store.load_chat(cid).archived is False  # un-archived on disk


async def test_restore_rebuilds_from_disk_when_not_in_memory(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P5 — a chat archived in a PRIOR session isn't in the live registry; restore
    rebuilds the ChatSession from its persisted record (history carried)."""
    chat_store.save_chat(ChatRecord(
        chat_id="d" * 32, session_id="x", title="Old", archived=True,
        created_at="2026-06-30T10:00:00", started_at="2026-06-30T10:00:00",
        history=[{"role": "user", "content": "prev"}],
    ))
    s, sent = _session()
    assert "d" * 32 not in s.chats
    monkeypatch.setattr(chat_lifecycle_mod, "new_chat_session", lambda app, sess, **kw: SimpleNamespace(history=[]))

    await ws_mod._handle_chat_restore({}, s, {"chat_id": "d" * 32})

    assert "d" * 32 in s.chat_order
    assert s.active_chat_id == "d" * 32
    assert s.chats["d" * 32].history == [{"role": "user", "content": "prev"}]
    assert chat_store.load_chat("d" * 32).archived is False
    assert _last(sent)["type"] == "chat_restored"


async def test_restore_open_chat_fails_not_archived(_home: Path) -> None:
    s, sent = _session()
    await ws_mod._handle_chat_restore({}, s, {"chat_id": s.active_chat_id})
    assert _last(sent)["type"] == "chat_restore_failed"
    assert _last(sent)["data"]["reason"] == "not_archived"


async def test_restore_unknown_fails(_home: Path) -> None:
    s, sent = _session()
    await ws_mod._handle_chat_restore({}, s, {"chat_id": "f" * 32})
    assert _last(sent)["type"] == "chat_restore_failed"


def test_session_chat_summary_counts_all_chats() -> None:
    # Close-log must report turns across ALL chats, not just the active one.
    session = SimpleNamespace(chats={
        "a": SimpleNamespace(history=[{}, {}, {}, {}]),  # 2 user turns
        "b": SimpleNamespace(history=[{}, {}]),           # 1 user turn
        "c": SimpleNamespace(history=[]),                 # 0
    })
    assert ws_mod._session_chat_summary(session) == (3, 3)
