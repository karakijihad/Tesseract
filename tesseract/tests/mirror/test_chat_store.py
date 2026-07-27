"""mirror-multi-chat P1 — per-chat persistence (chat_store).

Offline unit tests for ``tesseract/sessions/chats/<chat_id>.json`` round-trip,
listing, archive/rename/delete, and chat_id validation. TESSERACT_HOME is
redirected to tmp_path so nothing touches the real sessions tree.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from types import SimpleNamespace

from tesseract.mirror.server import chat_store
from tesseract.mirror.server.chat_store import ChatRecord
from tesseract.mirror.server.session import ChatMeta


def _write_with_ended_at(record: ChatRecord, ended_at: str | None) -> None:
    """Bypass ``save_chat``'s auto-stamped ``ended_at`` so a test can pin
    a chat's "last activity" to an arbitrary date, independent of the
    real wall clock the test happens to run on."""
    record.ended_at = ended_at
    directory = chat_store.chats_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{record.chat_id}.json").write_text(
        json.dumps(record.to_dict(), indent=2), encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # chat_store resolves its dir at call time, so setting the env is enough.
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


_CID = "0123456789abcdef0123456789abcdef"


def _record(chat_id: str = _CID, *, title: str = "2026-06-27 09:00", archived: bool = False) -> ChatRecord:
    return ChatRecord(
        chat_id=chat_id,
        session_id="test-session",
        title=title,
        created_at="2026-06-27T09:00:00+00:00",
        started_at="2026-06-27T09:00:00+00:00",
        archived=archived,
        history=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ],
    )


def test_save_then_load_round_trip(_home: Path) -> None:
    path = chat_store.save_chat(_record())
    assert path == chat_store.chats_dir() / f"{_CID}.json"
    assert path.exists()
    loaded = chat_store.load_chat(_CID)
    assert loaded is not None
    assert loaded.chat_id == _CID
    assert loaded.session_id == "test-session"
    assert loaded.title == "2026-06-27 09:00"
    assert loaded.archived is False
    assert loaded.turn_count == 1  # one user message
    assert len(loaded.history) == 2
    assert loaded.ended_at  # stamped on save


def test_load_missing_returns_none(_home: Path) -> None:
    assert chat_store.load_chat(_CID) is None


def test_save_does_not_mutate_caller_record(_home: Path) -> None:
    rec = _record()
    rec.history = [{"role": "user", "content": [{"type": "image", "data": "BYTES"}]}]
    before = [dict(m) for m in rec.history]
    chat_store.save_chat(rec)
    # caller's record is untouched — history not stripped, ended_at not stamped
    assert rec.history == before
    assert rec.ended_at is None


def test_save_strips_attachment_bytes(_home: Path) -> None:
    rec = _record()
    rec.history = [
        {"role": "user", "content": [{"type": "image", "data": "BASE64BYTES", "mime": "image/png"}]},
    ]
    chat_store.save_chat(rec)
    loaded = chat_store.load_chat(_CID)
    assert loaded is not None
    part = loaded.history[0]["content"][0]
    assert "data" not in part
    assert part["mime"] == "image/png"


def test_list_excludes_archived_by_default(_home: Path) -> None:
    a = "a" * 32
    b = "b" * 32
    chat_store.save_chat(_record(a, title="active"))
    chat_store.save_chat(_record(b, title="gone", archived=True))
    rows = chat_store.list_chats()
    ids = {r["chat_id"] for r in rows}
    assert a in ids
    assert b not in ids
    all_rows = chat_store.list_chats(include_archived=True)
    assert {r["chat_id"] for r in all_rows} == {a, b}
    # rows carry sidebar metadata, not full history
    assert "history" not in rows[0]
    assert rows[0]["message_count"] == 2


def test_set_archived_flips_flag(_home: Path) -> None:
    chat_store.save_chat(_record())
    assert chat_store.set_archived(_CID, True) is True
    loaded = chat_store.load_chat(_CID)
    assert loaded is not None and loaded.archived is True
    assert chat_store.set_archived("f" * 32, True) is False  # missing


def test_rename_chat(_home: Path) -> None:
    chat_store.save_chat(_record())
    assert chat_store.rename_chat(_CID, "renamed") is True
    loaded = chat_store.load_chat(_CID)
    assert loaded is not None and loaded.title == "renamed"
    assert chat_store.rename_chat("f" * 32, "x") is False


def test_delete_chat(_home: Path) -> None:
    chat_store.save_chat(_record())
    ok, reason = chat_store.delete_chat(_CID)
    assert ok is True and reason == ""
    assert chat_store.load_chat(_CID) is None
    ok2, reason2 = chat_store.delete_chat(_CID)
    assert ok2 is False and reason2 == "not_found"


def test_persist_session_chats_round_trips_open_and_archived(_home: Path) -> None:
    a, b = "a" * 32, "b" * 32
    session = SimpleNamespace(
        session_id="test-session",
        chats={
            a: SimpleNamespace(history=[{"role": "user", "content": "hi"}]),
            b: SimpleNamespace(history=[]),
        },
        chat_meta={
            a: ChatMeta(chat_id=a, title="open", created_at="t0", started_at="t0"),
            b: ChatMeta(chat_id=b, title="gone", created_at="t0", started_at="t0", archived=True),
        },
    )
    assert chat_store.persist_session_chats(session) == 2
    assert {r["chat_id"] for r in chat_store.list_chats()} == {a}
    assert {r["chat_id"] for r in chat_store.list_chats(include_archived=True)} == {a, b}
    assert chat_store.load_chat(b).archived is True


def test_index_session_chats_indexes_each_chat_by_id(_home: Path) -> None:
    from tesseract.memory.work_index import WorkIndex

    a, b = "a" * 32, "b" * 32
    session = SimpleNamespace(
        session_id="test-session",
        chats={
            a: SimpleNamespace(history=[{"role": "user", "content": "alpha apple chat one"}]),
            b: SimpleNamespace(history=[{"role": "user", "content": "beta banana chat two"}]),
        },
        chat_meta={
            a: ChatMeta(chat_id=a, title="one", created_at="t0", started_at="t0"),
            b: ChatMeta(chat_id=b, title="two", created_at="t0", started_at="t0"),
        },
    )
    assert chat_store.persist_session_chats(session) == 2
    # Each chat is recall-indexed separately, keyed by its own chat_id — not
    # collapsed under one session entry.
    assert chat_store.index_session_chats(session) == 2
    idx = WorkIndex(_home / "work_index.sqlite")
    try:
        assert {h.source_ref for h in idx.search("alpha", source="session")} == {a}
        assert {h.source_ref for h in idx.search("beta", source="session")} == {b}
    finally:
        idx.close()


def test_index_session_chats_skips_empty_chats(_home: Path) -> None:
    a = "a" * 32
    session = SimpleNamespace(
        session_id="test-session",
        chats={a: SimpleNamespace(history=[])},
        chat_meta={a: ChatMeta(chat_id=a, title="empty", created_at="t0", started_at="t0")},
    )
    chat_store.persist_session_chats(session)
    assert chat_store.index_session_chats(session) == 0


def test_archive_stale_open_chats_archives_prior_day_only(_home: Path) -> None:
    a, b = "a" * 32, "b" * 32
    _write_with_ended_at(_record(a, title="stale"), "2026-07-04T09:00:00+00:00")
    _write_with_ended_at(_record(b, title="fresh"), "2026-07-05T09:00:00+00:00")

    archived = chat_store.archive_stale_open_chats(today="2026-07-05")

    assert archived == 1
    assert chat_store.load_chat(a).archived is True
    assert chat_store.load_chat(b).archived is False


def test_archive_stale_open_chats_uses_message_timestamp_over_bulk_refreshed_ended_at(
    _home: Path,
) -> None:
    """Code-review finding: `persist_session_chats` re-stamps `ended_at` for
    EVERY open chat in a session on any single chat's save (create/rename/
    archive/autosave) — not just the one that changed. A chat the operator
    hasn't touched in days can end up with `ended_at` = today the moment a
    sibling chat is saved. The chat's own last-message timestamp is immune
    to that and must win, so this stale chat is still archived."""
    a = "a" * 32
    rec = _record(a, title="laundered")
    rec.history = [
        {"role": "user", "content": "old business", "timestamp": "2026-07-04T09:00:00+00:00"},
        {"role": "assistant", "content": "ack", "timestamp": "2026-07-04T09:00:05+00:00"},
    ]
    # ended_at says "today" (as if a sibling chat's save just bulk-refreshed
    # it), but the last real message in THIS chat was yesterday.
    _write_with_ended_at(rec, "2026-07-05T08:00:00+00:00")

    archived = chat_store.archive_stale_open_chats(today="2026-07-05")

    assert archived == 1
    assert chat_store.load_chat(a).archived is True


def test_archive_stale_open_chats_leaves_unparseable_timestamp_open(_home: Path) -> None:
    a = "a" * 32
    rec = _record(a, title="no-date")
    rec.created_at = ""
    rec.started_at = ""
    _write_with_ended_at(rec, None)

    archived = chat_store.archive_stale_open_chats(today="2026-07-05")

    assert archived == 0
    assert chat_store.load_chat(a).archived is False


def test_archive_stale_open_chats_skips_already_archived(_home: Path) -> None:
    a = "a" * 32
    _write_with_ended_at(_record(a, title="stale", archived=True), "2026-07-04T09:00:00+00:00")

    archived = chat_store.archive_stale_open_chats(today="2026-07-05")

    assert archived == 0  # list_chats() excludes archived by default — nothing to touch


@pytest.mark.parametrize("bad", ["../etc", "abc", "x" * 31, "g" * 32, "0123/56789abcdef0123456789abcdef0"])
def test_invalid_chat_id_rejected(_home: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        chat_store.save_chat(_record(bad))
    assert chat_store.load_chat(bad) is None
    assert chat_store.set_archived(bad, True) is False
    ok, reason = chat_store.delete_chat(bad)
    assert ok is False and reason == "invalid_id"
