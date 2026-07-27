"""Session 2 (2026-05-16) — channel_send_{voice,photo,document} kernel tools.

1. Each tool dispatches to the live adapter resolved via
   ``integrations.get_channel``.
2. Missing adapter (channel not registered) returns ``ToolResult(is_error=True)``
   with a clear message instead of crashing.
3. Mutex args (exactly-one source) enforced before adapter call.
4. Adapter exceptions surface as ``ToolResult(is_error=True)`` strings.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tesseract.integrations import register_channel, unregister_channel
from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.channel_send import (
    ChannelSendDocumentInput,
    ChannelSendDocumentTool,
    ChannelSendPhotoInput,
    ChannelSendPhotoTool,
    ChannelSendVoiceInput,
    ChannelSendVoiceTool,
)


@pytest.fixture
def fake_adapter():
    """Register a mock telegram adapter for the duration of one test."""
    adapter = MagicMock()
    adapter.send_voice = AsyncMock(return_value={"message_id": 101})
    adapter.send_photo = AsyncMock(return_value={"message_id": 102})
    adapter.send_document = AsyncMock(return_value={"message_id": 103})
    # Pin the protocol-required attrs so isinstance(adapter, ChannelAdapter)
    # passes — the registry's `runtime_checkable` Protocol check inspects
    # the surface.
    adapter.name = "telegram"
    adapter.start = AsyncMock()
    adapter.stop = AsyncMock()
    adapter.status_snapshot = MagicMock()
    adapter.list_users = MagicMock(return_value=[])
    adapter.approve = AsyncMock()
    adapter.revoke = AsyncMock()
    adapter.block = AsyncMock()
    adapter.list_conversation = MagicMock(return_value=[])
    register_channel(adapter)
    try:
        yield adapter
    finally:
        unregister_channel("telegram")


@pytest.mark.asyncio
async def test_channel_send_voice_dispatches_to_adapter(fake_adapter) -> None:
    tool = ChannelSendVoiceTool()
    result = await tool.run(
        ChannelSendVoiceInput(
            channel="telegram", chat_ref="99",
            text="hello", reply_to_message_id=42,
        ),
        ToolContext(),
    )
    assert not result.is_error
    assert "telegram:99" in result.output
    assert "message_id=101" in result.output
    fake_adapter.send_voice.assert_awaited_once_with(
        chat_ref="99", text="hello", audio_bytes=None,
        caption=None, reply_to_message_id=42,
    )


@pytest.mark.asyncio
async def test_channel_send_voice_requires_exactly_one_source(fake_adapter) -> None:
    tool = ChannelSendVoiceTool()
    # Neither set.
    result = await tool.run(
        ChannelSendVoiceInput(channel="telegram", chat_ref="99"),
        ToolContext(),
    )
    assert result.is_error
    assert "exactly one" in result.output
    fake_adapter.send_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_send_photo_dispatches_with_source_url(fake_adapter) -> None:
    tool = ChannelSendPhotoTool()
    result = await tool.run(
        ChannelSendPhotoInput(
            channel="telegram", chat_ref="99",
            source_url="https://example.com/cat.jpg",
            caption="meow",
        ),
        ToolContext(),
    )
    assert not result.is_error
    fake_adapter.send_photo.assert_awaited_once_with(
        chat_ref="99", source_path=None,
        source_url="https://example.com/cat.jpg",
        caption="meow", reply_to_message_id=None,
    )


@pytest.mark.asyncio
async def test_channel_send_document_passes_through(fake_adapter, tmp_path) -> None:
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-1.4")
    tool = ChannelSendDocumentTool()
    result = await tool.run(
        ChannelSendDocumentInput(
            channel="telegram", chat_ref="99",
            source_path=str(doc), caption="report",
        ),
        ToolContext(),
    )
    assert not result.is_error
    fake_adapter.send_document.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_channel_returns_clean_error() -> None:
    """No adapter registered → clean error, not a crash."""
    tool = ChannelSendVoiceTool()
    result = await tool.run(
        ChannelSendVoiceInput(
            channel="signal", chat_ref="99", text="hi",
        ),
        ToolContext(),
    )
    assert result.is_error
    assert "not registered" in result.output


@pytest.mark.asyncio
async def test_adapter_exception_becomes_tool_error(fake_adapter) -> None:
    fake_adapter.send_voice = AsyncMock(side_effect=RuntimeError("network down"))
    tool = ChannelSendVoiceTool()
    result = await tool.run(
        ChannelSendVoiceInput(
            channel="telegram", chat_ref="99", text="hi",
        ),
        ToolContext(),
    )
    assert result.is_error
    assert "network down" in result.output


def test_all_three_tools_default_to_auto_posture() -> None:
    """Outbound media is the expected reply path — should not gate by default."""
    assert ChannelSendVoiceTool.default_posture == "auto"
    assert ChannelSendPhotoTool.default_posture == "auto"
    assert ChannelSendDocumentTool.default_posture == "auto"
