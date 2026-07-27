"""mirror-multi-chat — chat sidebar timestamps are stamped in the operator's
local zone, not UTC.

The dropdown (``ChatManager``) renders a chat's default title verbatim
(``_new_chat_meta``'s ``%Y-%m-%d %H:%M``) and sorts by ``created_at``. Both are
derived from the ``now`` the ServerSession stamps at creation, so a chat opened
at 19:34 local must read "19:34" — not "17:34" from a UTC stamp.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from tesseract.mirror.server.session import ServerSession


def _session() -> ServerSession:
    return ServerSession(
        session_id="sess-test",
        ws=SimpleNamespace(closed=False),
        chat_session=SimpleNamespace(tag="c0", history=[]),
        event_log=SimpleNamespace(append=lambda *_: None),
    )


def _local_offset():
    return datetime.now().astimezone().utcoffset()


def test_seeded_chat_created_at_uses_local_zone() -> None:
    s = _session()
    meta = s.chat_meta[s.active_chat_id]
    parsed = datetime.fromisoformat(meta.created_at)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == _local_offset()


def test_created_chat_title_matches_local_wall_clock() -> None:
    s = _session()
    cid = s.create_chat(SimpleNamespace(tag="c1", history=[]))
    meta = s.chat_meta[cid]
    parsed = datetime.fromisoformat(meta.created_at)
    assert parsed.utcoffset() == _local_offset()
    # Title is the local wall-clock of the same stamp — no UTC skew.
    assert meta.title == parsed.strftime("%Y-%m-%d %H:%M")
