"""CR-2B integration: the Telegram bridge dispatches document attachments
to :func:`extract_document_text`, surfaces ``too_large`` against the
configured cap, and persists the typed attachments alongside the
rendered envelope on the conversation log row.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tesseract.integrations._channel_attachment import ChannelAttachment
from tesseract.integrations._channels_config import ChannelsConfig
from tesseract.integrations._handlers.document import DocumentHandlerError
from tesseract.integrations._retention import RetentionPolicy
from tesseract.integrations.telegram.api import TelegramMessage
from tesseract.integrations.telegram.bridge import TelegramBridge
from tesseract.integrations.telegram.download import (
    FetchRejection,
    FetchedAttachment,
)
from tesseract.integrations.telegram.state import StateBundle, save_allowlist


def _build_bridge(tmp_path, monkeypatch, *, channels_cfg=None):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge._api = MagicMock()
    bridge._api.send_chat_action = AsyncMock()
    bridge._api.send_message = AsyncMock(return_value={"message_id": 5})
    bridge._api.edit_message_text = AsyncMock()
    bridge._conversations = MagicMock()
    bridge._app = {
        "channels_config": channels_cfg or ChannelsConfig(),
        "stt_engine": None,
        "cost_ledger": None,
    }
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
    bridge._state.allowlist.chat_ids.add(99)
    save_allowlist(bridge._state.allowlist_path, bridge._state.allowlist)
    return bridge


def _stub_turn(monkeypatch, captured: dict) -> None:
    async def fake_start(app, session, *, channel, chat_id, body, on_progress=None):
        del app, session, channel, chat_id
        captured["body"] = body
        return ""

    monkeypatch.setattr(
        "tesseract.mirror.server.ws._start_channel_turn", fake_start
    )


def _stub_session_and_retention(monkeypatch, bridge) -> None:
    monkeypatch.setattr(bridge, "_session_for", lambda chat_id, reset: MagicMock())
    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.apply_retention_inplace",
        lambda session, policy, **kwargs: None,
    )


def _msg(att: ChannelAttachment, *, text: str = "") -> TelegramMessage:
    return TelegramMessage(
        update_id=1,
        message_id=7,
        chat_id=99,
        chat_type="private",
        from_user_id=11,
        from_username="jane.doe",
        text=text,
        date=100,
        attachments=(att,),
    )


@pytest.mark.asyncio
async def test_pdf_attachment_extracts_to_ready(tmp_path, monkeypatch) -> None:
    bridge = _build_bridge(tmp_path, monkeypatch)

    async def fake_fetch(file_id, *, api, max_bytes):
        del api, max_bytes
        assert file_id == "D1"
        return FetchedAttachment(data=b"%PDF-1.4 fake", size=12)

    async def fake_extract(data, *, mime, filename, max_chars):
        assert mime == "application/pdf"
        assert filename == "report.pdf"
        return "Q4 revenue grew 12%."

    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.fetch_telegram_attachment",
        fake_fetch,
    )
    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.extract_document_text",
        fake_extract,
    )

    captured: dict = {}
    _stub_turn(monkeypatch, captured)
    _stub_session_and_retention(monkeypatch, bridge)

    doc = ChannelAttachment(
        kind="document",
        status="no_handler",
        source="telegram",
        mime="application/pdf",
        size=12,
        filename="report.pdf",
        ref="D1",
    )
    await bridge._handle_message(_msg(doc, text="summarize this"))

    body = captured["body"]
    assert body.startswith("summarize this\n\n")
    assert 'status="ready"' in body
    assert "Q4 revenue grew 12%." in body


@pytest.mark.asyncio
async def test_document_over_byte_cap_skips_fetch(tmp_path, monkeypatch) -> None:
    bridge = _build_bridge(tmp_path, monkeypatch)

    fetch_calls: list = []

    async def fake_fetch(file_id, *, api, max_bytes):
        fetch_calls.append(file_id)
        return FetchedAttachment(data=b"", size=0)

    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.fetch_telegram_attachment",
        fake_fetch,
    )

    captured: dict = {}
    _stub_turn(monkeypatch, captured)
    _stub_session_and_retention(monkeypatch, bridge)

    over_cap = 26_214_400 * 2  # default cap is 25 MiB
    doc = ChannelAttachment(
        kind="document",
        status="no_handler",
        source="telegram",
        mime="application/pdf",
        size=over_cap,
        filename="big.pdf",
        ref="D1",
    )
    await bridge._handle_message(_msg(doc))

    body = captured["body"]
    assert 'status="too_large"' in body
    assert "exceeds" in body
    assert fetch_calls == []


@pytest.mark.asyncio
async def test_unsupported_mime_marks_extract_failed(tmp_path, monkeypatch) -> None:
    bridge = _build_bridge(tmp_path, monkeypatch)

    async def fake_fetch(file_id, *, api, max_bytes):
        del file_id, api, max_bytes
        return FetchedAttachment(data=b"binary blob", size=11)

    async def fake_extract(data, *, mime, filename, max_chars):
        raise DocumentHandlerError("no extractor for document mime/suffix=application/zip")

    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.fetch_telegram_attachment",
        fake_fetch,
    )
    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.extract_document_text",
        fake_extract,
    )

    captured: dict = {}
    _stub_turn(monkeypatch, captured)
    _stub_session_and_retention(monkeypatch, bridge)

    doc = ChannelAttachment(
        kind="document",
        status="no_handler",
        source="telegram",
        mime="application/zip",
        filename="archive.zip",
        ref="D1",
    )
    await bridge._handle_message(_msg(doc))

    body = captured["body"]
    assert 'status="extract_failed"' in body
    assert "no extractor" in body
    assert "application/zip" in body


@pytest.mark.asyncio
async def test_document_empty_extracted_text_marks_extract_failed(
    tmp_path, monkeypatch
) -> None:
    bridge = _build_bridge(tmp_path, monkeypatch)

    async def fake_fetch(file_id, *, api, max_bytes):
        del file_id, api, max_bytes
        return FetchedAttachment(data=b"%PDF-fake", size=9)

    async def fake_extract(data, *, mime, filename, max_chars):
        return "   "

    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.fetch_telegram_attachment",
        fake_fetch,
    )
    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.extract_document_text",
        fake_extract,
    )

    captured: dict = {}
    _stub_turn(monkeypatch, captured)
    _stub_session_and_retention(monkeypatch, bridge)

    doc = ChannelAttachment(
        kind="document",
        status="no_handler",
        source="telegram",
        mime="application/pdf",
        filename="empty.pdf",
        ref="D1",
    )
    await bridge._handle_message(_msg(doc))

    body = captured["body"]
    assert 'status="extract_failed"' in body
    assert "empty" in body


@pytest.mark.asyncio
async def test_conversation_log_records_typed_attachments(
    tmp_path, monkeypatch
) -> None:
    bridge = _build_bridge(tmp_path, monkeypatch)

    async def fake_fetch(file_id, *, api, max_bytes):
        del file_id, api, max_bytes
        return FetchedAttachment(data=b"%PDF-fake", size=9)

    async def fake_extract(data, *, mime, filename, max_chars):
        return "snippet"

    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.fetch_telegram_attachment",
        fake_fetch,
    )
    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.extract_document_text",
        fake_extract,
    )

    captured: dict = {}
    _stub_turn(monkeypatch, captured)
    _stub_session_and_retention(monkeypatch, bridge)

    doc = ChannelAttachment(
        kind="document",
        status="no_handler",
        source="telegram",
        mime="application/pdf",
        filename="report.pdf",
        ref="D1",
    )
    await bridge._handle_message(_msg(doc, text="see attached"))

    appended = bridge._conversations.append.call_args
    assert appended is not None
    args, _ = appended
    channel_message = args[2]
    assert len(channel_message.attachments) == 1
    typed = channel_message.attachments[0]
    assert typed.kind == "document"
    assert typed.status == "ready"
    assert typed.extracted == "snippet"
    # Body still carries the rendered envelope for back-compat readers.
    assert "<channel_attachment" in channel_message.body
    assert "<extracted>" in channel_message.body


@pytest.mark.asyncio
async def test_jpeg_as_document_routes_to_image_extractor(tmp_path, monkeypatch) -> None:
    """2026-05-15 — JPEG sent as Telegram document (drag-drop file) lands as
    kind=document mime=image/jpeg. Bridge must route to the vision extractor
    instead of failing with `no extractor for document mime/suffix=image/jpeg`.
    """
    bridge = _build_bridge(tmp_path, monkeypatch)

    async def fake_fetch(file_id, *, api, max_bytes):
        del api, max_bytes
        assert file_id == "D2"
        return FetchedAttachment(data=b"\xff\xd8\xff\xe0jpegbody", size=12)

    image_calls: list = []

    async def fake_describe(data, *, mime, caption, cost_ledger, max_chars):
        del data, cost_ledger
        image_calls.append((mime, caption, max_chars))
        return "A medical station with multiple screens."

    extract_calls: list = []

    async def fake_extract(data, *, mime, filename, max_chars):
        del data, mime, filename, max_chars
        extract_calls.append("called")
        return "should not be reached"

    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.fetch_telegram_attachment",
        fake_fetch,
    )
    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.describe_image",
        fake_describe,
    )
    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.extract_document_text",
        fake_extract,
    )

    captured: dict = {}
    _stub_turn(monkeypatch, captured)
    _stub_session_and_retention(monkeypatch, bridge)

    doc = ChannelAttachment(
        kind="document",
        status="no_handler",
        source="telegram",
        mime="image/jpeg",
        size=12,
        filename="MedStation.jpg",
        ref="D2",
    )
    await bridge._handle_message(_msg(doc, text="what's this"))

    assert image_calls and image_calls[0][0] == "image/jpeg"
    assert extract_calls == []
    body = captured["body"]
    assert 'status="ready"' in body
    assert "medical station" in body.lower()
