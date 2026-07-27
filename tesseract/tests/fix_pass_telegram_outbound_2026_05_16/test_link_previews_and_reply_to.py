"""Session 2 (2026-05-16) — link previews ON + reply_to_message_id plumbing.

1. ``send_message`` default flips link previews ON (was forced off);
   ``edit_message_text`` default stays OFF (progress-narrative churn would
   thrash Telegram's preview cache).
2. Per-call ``disable_web_page_preview`` override flows into the payload.
3. ``reply_to_message_id`` passes through every layer:
   ``TelegramAPI.send_message`` → ``_send_fresh`` → ``_send_outbound``.
4. ``_send_outbound`` attaches ``reply_to`` to the FIRST fresh chunk only
   (subsequent chunks omit it so the thread doesn't repeat the quote).
5. Bridge auto-attaches ``reply_to_message_id`` ONLY when the placeholder
   send failed (placeholder edit is the visual anchor when it works).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tesseract.integrations.telegram.api import TelegramAPI


@pytest.mark.asyncio
async def test_send_message_defaults_to_link_previews_on() -> None:
    api = TelegramAPI(token="x" * 40)
    captured: dict[str, Any] = {}

    async def fake_call(method, payload, *, read_timeout=None):
        del read_timeout
        captured["method"] = method
        captured["payload"] = payload
        return {"message_id": 1}

    api._call = fake_call  # type: ignore[method-assign]
    await api.send_message(chat_id=99, text="check out https://example.com")
    assert captured["method"] == "sendMessage"
    assert captured["payload"]["disable_web_page_preview"] is False
    await api.aclose()


@pytest.mark.asyncio
async def test_send_message_per_call_override_disables_preview() -> None:
    api = TelegramAPI(token="x" * 40)
    captured: dict[str, Any] = {}

    async def fake_call(method, payload, *, read_timeout=None):
        del method, read_timeout
        captured["payload"] = payload
        return {"message_id": 2}

    api._call = fake_call  # type: ignore[method-assign]
    await api.send_message(
        chat_id=99, text="quiet status ping",
        disable_web_page_preview=True,
    )
    assert captured["payload"]["disable_web_page_preview"] is True
    await api.aclose()


@pytest.mark.asyncio
async def test_edit_message_text_defaults_to_link_previews_off() -> None:
    api = TelegramAPI(token="x" * 40)
    captured: dict[str, Any] = {}

    async def fake_call(method, payload, *, read_timeout=None):
        del read_timeout
        captured["method"] = method
        captured["payload"] = payload
        return {"message_id": 3}

    api._call = fake_call  # type: ignore[method-assign]
    await api.edit_message_text(chat_id=99, message_id=3, text="progress…")
    assert captured["method"] == "editMessageText"
    # Edits keep previews OFF — progress edits churn many times per turn.
    assert captured["payload"]["disable_web_page_preview"] is True
    await api.aclose()


@pytest.mark.asyncio
async def test_send_message_threads_reply_to_message_id() -> None:
    api = TelegramAPI(token="x" * 40)
    captured: dict[str, Any] = {}

    async def fake_call(method, payload, *, read_timeout=None):
        del method, read_timeout
        captured["payload"] = payload
        return {"message_id": 7}

    api._call = fake_call  # type: ignore[method-assign]
    await api.send_message(chat_id=99, text="quote-reply", reply_to_message_id=42)
    assert captured["payload"]["reply_to_message_id"] == 42
    await api.aclose()


@pytest.mark.asyncio
async def test_send_outbound_attaches_reply_to_only_to_first_fresh_chunk() -> None:
    """Multi-chunk reply with no placeholder: chunk 0 carries reply_to,
    chunks 1+ omit it so the thread doesn't repeat the quote header."""
    from tesseract.integrations.telegram.bridge import TelegramBridge
    from tesseract.integrations.telegram.state import StateBundle, save_allowlist

    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge._api = MagicMock()
    bridge._api.send_message = AsyncMock(return_value={"message_id": 100})
    bridge._api.edit_message_text = AsyncMock()
    bridge._conversations = MagicMock()
    bridge._app = MagicMock()

    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "telegram"
        state_dir.mkdir(parents=True, exist_ok=True)
        bridge._state = StateBundle(dir_path=state_dir)
        bridge._state.allowlist.chat_ids.add(99)
        save_allowlist(bridge._state.allowlist_path, bridge._state.allowlist)

        # 8000-char body forces a chunked split (chunker cap is 4000).
        long_body = "x" * 7800
        await bridge._send_outbound(
            chat_id=99, body=long_body,
            placeholder_id=None,  # no placeholder → reply_to lands on chunk 0
            reply_to_message_id=42,
        )

    calls = bridge._api.send_message.await_args_list
    assert len(calls) >= 2  # chunked
    # Only the FIRST send carries reply_to_message_id.
    first_kwargs = calls[0].kwargs
    second_kwargs = calls[1].kwargs
    assert first_kwargs.get("reply_to_message_id") == 42
    assert second_kwargs.get("reply_to_message_id") is None
