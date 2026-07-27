"""mirror-multi-chat P1 — ServerSession multi-chat data model.

Pure-Python unit tests for the chat registry on ``ServerSession``: seeding,
create, switch, archive, and the tab cap (D5). No WS / aiohttp / disk — the
model methods only register and swap ``ChatSession`` objects, so plain
``SimpleNamespace`` fakes stand in for the brain-layer sessions.

Single-chat behaviour must stay byte-identical: a ServerSession built the old
way (one ``chat_session=``) auto-seeds exactly one chat and ``.chat_session``
keeps pointing at it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tesseract.mirror.server.session import MAX_OPEN_CHATS, ServerSession


def _fake_cs(tag: str = "cs") -> SimpleNamespace:
    return SimpleNamespace(tag=tag, history=[])


def _session(chat_session: SimpleNamespace | None = None) -> ServerSession:
    return ServerSession(
        session_id="sess-test",
        ws=SimpleNamespace(closed=False),
        chat_session=chat_session or _fake_cs("c0"),
        event_log=SimpleNamespace(append=lambda *_: None),
    )


_T0 = datetime(2026, 6, 27, 9, 0, tzinfo=timezone.utc)


def test_post_init_seeds_single_chat() -> None:
    cs = _fake_cs("c0")
    s = _session(cs)
    assert len(s.chats) == 1
    assert s.active_chat_id in s.chats
    assert s.chat_order == [s.active_chat_id]
    assert s.chat_session is cs
    meta = s.chat_meta[s.active_chat_id]
    assert meta.archived is False
    assert meta.title  # non-empty default title


def test_create_chat_registers_without_switching() -> None:
    s = _session(_fake_cs("c0"))
    first = s.active_chat_id
    cs2 = _fake_cs("c1")
    cid = s.create_chat(cs2, now=_T0)
    assert cid in s.chats
    assert s.chats[cid] is cs2
    assert s.chat_order == [first, cid]
    # create does NOT auto-switch
    assert s.active_chat_id == first
    assert s.chat_session is not cs2
    assert s.chat_meta[cid].title == "2026-06-27 09:00"


def test_switch_chat_updates_active_and_pointer() -> None:
    s = _session(_fake_cs("c0"))
    cs2 = _fake_cs("c1")
    cid = s.create_chat(cs2)
    s.switch_chat(cid)
    assert s.active_chat_id == cid
    assert s.chat_session is cs2


def test_switch_unknown_chat_raises() -> None:
    s = _session()
    with pytest.raises(KeyError):
        s.switch_chat("does-not-exist")


def test_archive_non_active_removes_from_order_keeps_active() -> None:
    s = _session(_fake_cs("c0"))
    active = s.active_chat_id
    cid = s.create_chat(_fake_cs("c1"))
    s.archive_chat(cid)
    assert s.chat_meta[cid].archived is True
    assert cid not in s.chat_order
    assert s.active_chat_id == active  # untouched (D7-adjacent)


def test_archive_active_switches_to_remaining() -> None:
    s = _session(_fake_cs("c0"))
    active = s.active_chat_id
    cid = s.create_chat(_fake_cs("c1"))
    s.archive_chat(active)
    assert s.chat_meta[active].archived is True
    assert s.active_chat_id == cid
    assert s.chat_session is s.chats[cid]


def test_archive_last_active_chat_raises() -> None:
    s = _session()
    with pytest.raises(ValueError):
        s.archive_chat(s.active_chat_id)


def test_tab_cap_auto_archives_oldest(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _session(_fake_cs("seed"))
    seed = s.active_chat_id
    # Fill to the cap (seed + (cap-1) more = cap open chats).
    created = [s.create_chat(_fake_cs(f"c{i}")) for i in range(MAX_OPEN_CHATS - 1)]
    assert len(s.chat_order) == MAX_OPEN_CHATS
    # One more crosses the cap → oldest active (the seed) auto-archives.
    overflow = s.create_chat(_fake_cs("overflow"))
    assert len(s.chat_order) == MAX_OPEN_CHATS
    assert s.chat_meta[seed].archived is True
    assert seed not in s.chat_order
    assert overflow in s.chat_order
    assert created[0] in s.chat_order
