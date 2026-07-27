"""Session 2 (2026-05-16) — multipart upload methods on TelegramAPI.

1. ``send_voice`` POSTs to ``sendVoice`` with the OGG bytes in the
   ``voice`` field, optional caption / duration / reply_to passed as form
   data.
2. ``send_photo`` POSTs to ``sendPhoto`` with image bytes in ``photo``.
3. ``send_document`` POSTs to ``sendDocument`` with bytes in ``document``
   and the operator-supplied filename.
4. Non-2xx + non-``ok`` Telegram response raises ``TelegramAPIError``
   with the description string.
"""

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
async def test_send_voice_posts_multipart_to_sendvoice() -> None:
    api = TelegramAPI(token="x" * 40)
    api._client = MagicMock()
    api._client.post = AsyncMock(
        return_value=_fake_response({"ok": True, "result": {"message_id": 1}})
    )

    result = await api.send_voice(
        chat_id=99, ogg_opus_bytes=b"OggS\x00fake-ogg",
        caption="hello", duration_s=3, reply_to_message_id=42,
    )

    assert result == {"message_id": 1}
    call = api._client.post.await_args
    url = call.args[0]
    assert url.endswith("/sendVoice")
    assert call.kwargs["data"] == {
        "chat_id": "99",
        "caption": "hello",
        "duration": "3",
        "reply_to_message_id": "42",
    }
    voice_field = call.kwargs["files"]["voice"]
    assert voice_field[0] == "voice.ogg"
    assert voice_field[1] == b"OggS\x00fake-ogg"
    assert voice_field[2] == "audio/ogg"


@pytest.mark.asyncio
async def test_send_photo_posts_multipart_to_sendphoto() -> None:
    api = TelegramAPI(token="x" * 40)
    api._client = MagicMock()
    api._client.post = AsyncMock(
        return_value=_fake_response({"ok": True, "result": {"message_id": 2}})
    )

    await api.send_photo(
        chat_id=99, image_bytes=b"\xff\xd8jpeg",
        filename="cat.jpg", mime_type="image/jpeg", caption="meow",
    )

    call = api._client.post.await_args
    assert call.args[0].endswith("/sendPhoto")
    assert call.kwargs["data"] == {"chat_id": "99", "caption": "meow"}
    photo_field = call.kwargs["files"]["photo"]
    assert photo_field == ("cat.jpg", b"\xff\xd8jpeg", "image/jpeg")


@pytest.mark.asyncio
async def test_send_document_posts_multipart_to_senddocument() -> None:
    api = TelegramAPI(token="x" * 40)
    api._client = MagicMock()
    api._client.post = AsyncMock(
        return_value=_fake_response({"ok": True, "result": {"message_id": 3}})
    )

    await api.send_document(
        chat_id=99, document_bytes=b"%PDF-1.4",
        filename="report.pdf", mime_type="application/pdf",
    )

    call = api._client.post.await_args
    assert call.args[0].endswith("/sendDocument")
    doc_field = call.kwargs["files"]["document"]
    assert doc_field == ("report.pdf", b"%PDF-1.4", "application/pdf")


@pytest.mark.asyncio
async def test_multipart_failure_raises_telegram_api_error() -> None:
    api = TelegramAPI(token="x" * 40)
    api._client = MagicMock()
    api._client.post = AsyncMock(
        return_value=_fake_response(
            {"ok": False, "description": "file too large"}, status=400,
        )
    )

    with pytest.raises(TelegramAPIError, match="file too large"):
        await api.send_voice(
            chat_id=99, ogg_opus_bytes=b"x", caption=None, duration_s=None,
        )
