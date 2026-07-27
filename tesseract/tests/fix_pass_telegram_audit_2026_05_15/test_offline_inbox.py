"""Audit fix M1 — offline inbox + replay-on-online tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tesseract.integrations._retention import RetentionPolicy
from tesseract.integrations.telegram.api import TelegramMessage
from tesseract.integrations.telegram.bridge import TelegramBridge
from tesseract.integrations.telegram.state import (
    OfflineMessage,
    StateBundle,
    save_allowlist,
    save_status,
    Status,
)


def _bridge(tmp_path, monkeypatch, *, allow_chat: int | None = None) -> TelegramBridge:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge._api = MagicMock()
    bridge._api.send_chat_action = AsyncMock()
    bridge._api.send_message = AsyncMock(return_value={"message_id": 1})
    bridge._conversations = MagicMock()
    bridge._app = MagicMock()
    bridge._app.get = lambda k, default=None: default
    bridge._sessions = {}
    bridge._retention = RetentionPolicy.fallback()
    bridge._poll_task = None
    bridge._stop_event = MagicMock()
    bridge._started_at = ""
    bridge._last_poll_at = None
    bridge._error_count = 0

    state_dir = tmp_path / "telegram"
    state_dir.mkdir(parents=True, exist_ok=True)
    bridge._state = StateBundle(dir_path=state_dir)
    if allow_chat is not None:
        bridge._state.allowlist.chat_ids.add(allow_chat)
        save_allowlist(bridge._state.allowlist_path, bridge._state.allowlist)
    return bridge


@pytest.mark.asyncio
async def test_offline_message_enqueued_not_dropped(tmp_path, monkeypatch) -> None:
    bridge = _bridge(tmp_path, monkeypatch, allow_chat=111)
    save_status(bridge._state.status_path, Status(override="offline"))

    msg = TelegramMessage(
        update_id=1,
        message_id=42,
        chat_id=111,
        chat_type="private",
        from_user_id=7,
        from_username="someone",
        text="hello while you're away",
        date=0,
    )
    await bridge._handle_message(msg)

    # Reply reflects the new wording — "saved", not just "queued".
    sent_kwargs = bridge._api.send_message.call_args_list[0].kwargs
    assert "saved" in sent_kwargs["text"].lower()
    # Inbox has the entry.
    bucket = bridge._state.poll_state.offline_inbox.get("111") or []
    assert len(bucket) == 1
    assert bucket[0].text == "hello while you're away"
    assert bucket[0].telegram_message_id == 42


@pytest.mark.asyncio
async def test_drain_replays_through_handle_message(tmp_path, monkeypatch) -> None:
    bridge = _bridge(tmp_path, monkeypatch, allow_chat=222)
    bridge._state.poll_state.offline_inbox["222"] = [
        OfflineMessage(
            ts="2026-05-15T10:00:00+00:00",
            telegram_message_id=1001,
            text="first",
            from_user_id=9,
            from_username="me",
        ),
        OfflineMessage(
            ts="2026-05-15T10:01:00+00:00",
            telegram_message_id=1002,
            text="second",
            from_user_id=9,
            from_username="me",
        ),
    ]
    # Status is "online" by default; drain should pick up both rows and
    # surface them as replays. We stub `_handle_message` itself to a
    # call-counter so the test stays focused on the drain mechanics
    # rather than the full turn loop.
    counter = MagicMock()

    async def _fake_handle(message):
        counter(message)

    bridge._handle_message = _fake_handle  # type: ignore[assignment]
    drained = await bridge.drain_offline_inbox()
    assert drained == 2
    assert counter.call_count == 2
    # Inbox empty after drain.
    assert bridge._state.poll_state.offline_inbox.get("222") in (None, [])


@pytest.mark.asyncio
async def test_list_missed_returns_dicts(tmp_path, monkeypatch) -> None:
    bridge = _bridge(tmp_path, monkeypatch, allow_chat=333)
    bridge._state.poll_state.offline_inbox["333"] = [
        OfflineMessage(
            ts="2026-05-15T11:00:00+00:00",
            telegram_message_id=2001,
            text="missed",
            from_user_id=None,
            from_username=None,
        )
    ]
    rows = bridge.list_missed(333)
    assert len(rows) == 1
    assert rows[0]["telegram_message_id"] == 2001
    assert rows[0]["text"] == "missed"
