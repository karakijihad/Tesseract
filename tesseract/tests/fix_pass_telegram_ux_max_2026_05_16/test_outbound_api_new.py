"""Session 3 (2026-05-16) — TelegramAPI methods for video / sticker /
location / contact / poll / dice / media_group."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from tesseract.integrations.telegram.api import TelegramAPI, TelegramAPIError


def _fake_response(payload: dict[str, Any], status: int = 200) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.is_success = (200 <= status < 300)
    resp.status_code = status
    resp.json.return_value = payload
    return resp


@pytest.mark.asyncio
async def test_send_video_multipart_with_streaming_hint() -> None:
    api = TelegramAPI(token="x" * 40)
    api._client = MagicMock()
    api._client.post = AsyncMock(
        return_value=_fake_response({"ok": True, "result": {"message_id": 9}})
    )
    await api.send_video(
        chat_id=99, video_bytes=b"mp4bytes", duration_s=42,
        width=1280, height=720, caption="demo",
    )
    call = api._client.post.await_args
    assert call.args[0].endswith("/sendVideo")
    assert call.kwargs["data"]["duration"] == "42"
    assert call.kwargs["data"]["width"] == "1280"
    assert call.kwargs["data"]["height"] == "720"
    assert call.kwargs["data"]["supports_streaming"] == "true"
    assert call.kwargs["data"]["caption"] == "demo"
    assert call.kwargs["files"]["video"][1] == b"mp4bytes"


@pytest.mark.asyncio
async def test_send_animation_multipart() -> None:
    api = TelegramAPI(token="x" * 40)
    api._client = MagicMock()
    api._client.post = AsyncMock(
        return_value=_fake_response({"ok": True, "result": {"message_id": 10}})
    )
    await api.send_animation(chat_id=99, animation_bytes=b"gifbytes")
    call = api._client.post.await_args
    assert call.args[0].endswith("/sendAnimation")
    assert call.kwargs["files"]["animation"][1] == b"gifbytes"


@pytest.mark.asyncio
async def test_send_sticker_with_file_id_uses_json_call() -> None:
    api = TelegramAPI(token="x" * 40)
    captured: dict[str, Any] = {}

    async def fake_call(method, payload, *, read_timeout=None):
        del read_timeout
        captured["method"] = method
        captured["payload"] = payload
        return {"message_id": 11}

    api._call = fake_call  # type: ignore[method-assign]
    await api.send_sticker(chat_id=99, sticker="CAACAgIAAxk", emoji="🔥")
    assert captured["method"] == "sendSticker"
    assert captured["payload"]["sticker"] == "CAACAgIAAxk"
    assert captured["payload"]["emoji"] == "🔥"


@pytest.mark.asyncio
async def test_send_sticker_with_bytes_uses_multipart() -> None:
    api = TelegramAPI(token="x" * 40)
    api._client = MagicMock()
    api._client.post = AsyncMock(
        return_value=_fake_response({"ok": True, "result": {"message_id": 12}})
    )
    await api.send_sticker(chat_id=99, sticker=b"WEBPbytes", emoji="💯")
    call = api._client.post.await_args
    assert call.args[0].endswith("/sendSticker")
    assert call.kwargs["files"]["sticker"][1] == b"WEBPbytes"
    assert call.kwargs["files"]["sticker"][2] == "image/webp"


@pytest.mark.asyncio
async def test_send_location_posts_lat_lon() -> None:
    api = TelegramAPI(token="x" * 40)
    captured: dict[str, Any] = {}

    async def fake_call(method, payload, *, read_timeout=None):
        del read_timeout
        captured["method"] = method
        captured["payload"] = payload
        return {"message_id": 13}

    api._call = fake_call  # type: ignore[method-assign]
    await api.send_location(chat_id=99, latitude=45.4642, longitude=9.1900)
    assert captured["method"] == "sendLocation"
    assert captured["payload"]["latitude"] == 45.4642
    assert captured["payload"]["longitude"] == 9.1900


@pytest.mark.asyncio
async def test_send_poll_options_shape() -> None:
    api = TelegramAPI(token="x" * 40)
    captured: dict[str, Any] = {}

    async def fake_call(method, payload, *, read_timeout=None):
        del method, read_timeout
        captured["payload"] = payload
        return {"message_id": 14}

    api._call = fake_call  # type: ignore[method-assign]
    await api.send_poll(
        chat_id=99, question="lunch?", options=["pizza", "sushi", "salad"],
    )
    assert captured["payload"]["question"] == "lunch?"
    assert captured["payload"]["options"] == [
        {"text": "pizza"}, {"text": "sushi"}, {"text": "salad"},
    ]


@pytest.mark.asyncio
async def test_send_poll_rejects_too_few_options() -> None:
    api = TelegramAPI(token="x" * 40)
    with pytest.raises(TelegramAPIError, match="2-10"):
        await api.send_poll(chat_id=99, question="x", options=["one"])


@pytest.mark.asyncio
async def test_send_dice_default_emoji() -> None:
    api = TelegramAPI(token="x" * 40)
    captured: dict[str, Any] = {}

    async def fake_call(method, payload, *, read_timeout=None):
        del method, read_timeout
        captured["payload"] = payload
        return {"message_id": 15}

    api._call = fake_call  # type: ignore[method-assign]
    await api.send_dice(chat_id=99)
    assert captured["payload"]["emoji"] == "🎲"


@pytest.mark.asyncio
async def test_send_media_group_rejects_count_out_of_range() -> None:
    api = TelegramAPI(token="x" * 40)
    with pytest.raises(TelegramAPIError, match="2-10"):
        await api.send_media_group(chat_id=99, media=[{"type": "photo", "media": "x"}])
