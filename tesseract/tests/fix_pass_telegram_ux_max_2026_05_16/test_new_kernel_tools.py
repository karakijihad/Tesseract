"""Session 3 (2026-05-16) — kernel tools for new outbound + reactions."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tesseract.integrations import register_channel, unregister_channel
from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.channel_send import (
    ChannelReactInput,
    ChannelReactTool,
    ChannelSendAnimationTool,
    ChannelSendLocationInput,
    ChannelSendLocationTool,
    ChannelSendPollInput,
    ChannelSendPollTool,
    ChannelSendStickerInput,
    ChannelSendStickerTool,
    ChannelSendVideoNoteTool,
    ChannelSendVideoTool,
)


@pytest.fixture
def fake_adapter():
    adapter = MagicMock()
    adapter.send_video = AsyncMock(return_value={"message_id": 201})
    adapter.send_animation = AsyncMock(return_value={"message_id": 202})
    adapter.send_video_note = AsyncMock(return_value={"message_id": 203})
    adapter.send_sticker = AsyncMock(return_value={"message_id": 204})
    adapter.send_location = AsyncMock(return_value={"message_id": 205})
    adapter.send_poll = AsyncMock(return_value={"message_id": 206})
    adapter.react_to_message = AsyncMock()
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
async def test_send_video_tool_dispatches_with_source_url(fake_adapter) -> None:
    tool = ChannelSendVideoTool()
    result = await tool.run(
        tool.input_schema(
            chat_ref="99", source_url="https://example.com/v.mp4", caption="demo",
        ),
        ToolContext(),
    )
    assert not result.is_error
    fake_adapter.send_video.assert_awaited_once_with(
        chat_ref="99", source_path=None,
        source_url="https://example.com/v.mp4",
        reply_to_message_id=None, caption="demo",
    )


@pytest.mark.asyncio
async def test_send_video_tool_requires_one_source(fake_adapter) -> None:
    tool = ChannelSendVideoTool()
    result = await tool.run(
        tool.input_schema(chat_ref="99"),
        ToolContext(),
    )
    assert result.is_error
    assert "exactly one" in result.output


@pytest.mark.asyncio
async def test_send_animation_passes_caption(fake_adapter, tmp_path) -> None:
    gif = tmp_path / "wave.mp4"
    gif.write_bytes(b"mp4bytes")
    tool = ChannelSendAnimationTool()
    result = await tool.run(
        tool.input_schema(chat_ref="99", source_path=str(gif), caption="🌊"),
        ToolContext(),
    )
    assert not result.is_error
    fake_adapter.send_animation.assert_awaited_once()
    assert fake_adapter.send_animation.await_args.kwargs["caption"] == "🌊"


@pytest.mark.asyncio
async def test_send_video_note_omits_caption(fake_adapter, tmp_path) -> None:
    note = tmp_path / "note.mp4"
    note.write_bytes(b"mp4bytes")
    tool = ChannelSendVideoNoteTool()
    result = await tool.run(
        tool.input_schema(chat_ref="99", source_path=str(note)),
        ToolContext(),
    )
    assert not result.is_error
    # video_note adapter call should NOT include caption kwarg.
    call_kwargs = fake_adapter.send_video_note.await_args.kwargs
    assert "caption" not in call_kwargs


@pytest.mark.asyncio
async def test_send_sticker_file_id_path(fake_adapter) -> None:
    tool = ChannelSendStickerTool()
    result = await tool.run(
        ChannelSendStickerInput(
            chat_ref="99", sticker_file_id="CAACAgIAAxk", emoji="🔥",
        ),
        ToolContext(),
    )
    assert not result.is_error
    fake_adapter.send_sticker.assert_awaited_once_with(
        chat_ref="99", sticker="CAACAgIAAxk", emoji="🔥",
        reply_to_message_id=None,
    )


@pytest.mark.asyncio
async def test_send_sticker_requires_one_source(fake_adapter) -> None:
    tool = ChannelSendStickerTool()
    result = await tool.run(
        ChannelSendStickerInput(chat_ref="99"),
        ToolContext(),
    )
    assert result.is_error
    assert "exactly one" in result.output


@pytest.mark.asyncio
async def test_send_location_clamped_input() -> None:
    """Pydantic validators reject out-of-range lat/lon at construction."""
    with pytest.raises(Exception):  # ValidationError; class import is heavy
        ChannelSendLocationInput(chat_ref="99", latitude=200.0, longitude=0.0)


@pytest.mark.asyncio
async def test_send_location_happy_path(fake_adapter) -> None:
    tool = ChannelSendLocationTool()
    result = await tool.run(
        ChannelSendLocationInput(
            chat_ref="99", latitude=45.4642, longitude=9.19,
        ),
        ToolContext(),
    )
    assert not result.is_error
    fake_adapter.send_location.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_poll_validates_option_count() -> None:
    """Single-option poll rejected by Pydantic min_length."""
    with pytest.raises(Exception):
        ChannelSendPollInput(chat_ref="99", question="x", options=["solo"])


@pytest.mark.asyncio
async def test_send_poll_happy_path(fake_adapter) -> None:
    tool = ChannelSendPollTool()
    result = await tool.run(
        ChannelSendPollInput(
            chat_ref="99", question="lunch?", options=["a", "b", "c"],
        ),
        ToolContext(),
    )
    assert not result.is_error


@pytest.mark.asyncio
async def test_channel_react_dispatches_and_clears(fake_adapter) -> None:
    tool = ChannelReactTool()
    result = await tool.run(
        ChannelReactInput(chat_ref="99", message_id=42, emoji="👍"),
        ToolContext(),
    )
    assert not result.is_error
    fake_adapter.react_to_message.assert_awaited_once_with(
        chat_ref="99", message_id=42, emoji="👍",
    )
    # None clears.
    fake_adapter.react_to_message.reset_mock()
    result = await tool.run(
        ChannelReactInput(chat_ref="99", message_id=42, emoji=None),
        ToolContext(),
    )
    assert not result.is_error
    assert "cleared" in result.output.lower()


def test_all_new_tools_default_to_auto() -> None:
    for cls in [
        ChannelSendVideoTool, ChannelSendAnimationTool, ChannelSendVideoNoteTool,
        ChannelSendStickerTool, ChannelSendLocationTool, ChannelSendPollTool,
        ChannelReactTool,
    ]:
        assert cls.default_posture == "auto", f"{cls.__name__} should default to auto"
