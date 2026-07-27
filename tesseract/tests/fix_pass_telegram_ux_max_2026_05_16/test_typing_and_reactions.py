"""Session 3 (2026-05-16) — typing keepalive + message reactions + URL extract.

1. ``_typing_keepalive`` re-fires sendChatAction repeatedly and exits on
   cancel without raising.
2. ``set_message_reaction`` posts the right payload shape (reaction
   array vs empty list for clear).
3. ``react_to_message`` is the public surface and propagates errors.
4. ``find_urls`` extracts http(s) URLs, trims trailing punctuation,
   de-duplicates, and caps at 3.
5. ``extract_urls_to_context`` returns "" when TAVILY_API_KEY is unset.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tesseract.integrations._url_extract import (
    extract_urls_to_context,
    find_urls,
)
from tesseract.integrations.telegram.api import TelegramAPI


@pytest.mark.asyncio
async def test_typing_keepalive_refires_and_cancels_cleanly(monkeypatch) -> None:
    from tesseract.integrations.telegram.bridge import TelegramBridge
    bridge = TelegramBridge.__new__(TelegramBridge)
    api = MagicMock()
    api.send_chat_action = AsyncMock()
    bridge._api = api

    # Patch the bridge module's `asyncio.sleep` so the loop doesn't
    # actually wait 4 s. Capture the real sleep first so the patched
    # version doesn't infinite-loop on itself.
    import tesseract.integrations.telegram.bridge as bridge_mod
    real_sleep = asyncio.sleep

    async def fast_sleep(_):
        await real_sleep(0)

    monkeypatch.setattr(bridge_mod.asyncio, "sleep", fast_sleep)

    task = asyncio.create_task(bridge._typing_keepalive(99))
    # Let it cycle a few times via the real event loop.
    for _ in range(5):
        await real_sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # At least 2 firings (one before the first sleep, at least one more).
    assert api.send_chat_action.await_count >= 2
    for call in api.send_chat_action.await_args_list:
        assert call.kwargs["chat_id"] == 99
        assert call.kwargs["action"] == "typing"


@pytest.mark.asyncio
async def test_set_message_reaction_posts_emoji_array() -> None:
    api = TelegramAPI(token="x" * 40)
    captured: dict[str, Any] = {}

    async def fake_call(method, payload, *, read_timeout=None):
        del read_timeout
        captured["method"] = method
        captured["payload"] = payload
        return {}

    api._call = fake_call  # type: ignore[method-assign]

    await api.set_message_reaction(chat_id=99, message_id=42, emoji="💭")
    assert captured["method"] == "setMessageReaction"
    assert captured["payload"]["reaction"] == [{"type": "emoji", "emoji": "💭"}]

    # None emoji clears the reaction by sending empty list.
    captured.clear()
    await api.set_message_reaction(chat_id=99, message_id=42, emoji=None)
    assert captured["payload"]["reaction"] == []
    await api.aclose()


@pytest.mark.asyncio
async def test_react_to_message_public_surface_propagates_errors() -> None:
    """`_safe_react` swallows; `react_to_message` is the public path that
    surfaces failures so the kernel tool can wrap them."""
    from tesseract.integrations.telegram.bridge import TelegramBridge
    from tesseract.integrations.telegram.api import TelegramAPIError

    bridge = TelegramBridge.__new__(TelegramBridge)
    api = MagicMock()
    api.set_message_reaction = AsyncMock(side_effect=TelegramAPIError("EMOJI_INVALID"))
    bridge._api = api

    with pytest.raises(TelegramAPIError, match="EMOJI_INVALID"):
        await bridge.react_to_message(
            chat_ref="99", message_id=42, emoji="❤",
        )


def test_find_urls_basic_cases() -> None:
    assert find_urls("https://example.com please") == ["https://example.com"]
    # Trailing punctuation stripped.
    assert find_urls("see https://example.com.") == ["https://example.com"]
    # De-dup preserves order.
    assert find_urls("a https://e.com b https://e.com") == ["https://e.com"]
    # Multiple distinct.
    out = find_urls("read https://a.com and http://b.com and https://c.com")
    assert out == ["https://a.com", "http://b.com", "https://c.com"]
    # Cap at 3.
    out = find_urls(" ".join(f"https://x{i}.com" for i in range(10)))
    assert len(out) == 3
    # No URLs.
    assert find_urls("just plain text") == []
    assert find_urls("") == []
    assert find_urls(None) == []


@pytest.mark.asyncio
async def test_extract_urls_returns_empty_without_api_key(monkeypatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    out = await extract_urls_to_context(["https://example.com"])
    assert out == ""


@pytest.mark.asyncio
async def test_extract_urls_formats_results(monkeypatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")

    import httpx

    class FakeResponse:
        status_code = 200
        def json(self) -> dict:
            return {
                "results": [
                    {"url": "https://example.com", "raw_content": "Article body here."},
                ]
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            del args, kwargs
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def post(self, url, *, headers, json):
            del url, headers, json
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    out = await extract_urls_to_context(["https://example.com"])
    assert "URL CONTENT" in out
    assert "https://example.com" in out
    assert "Article body here." in out
