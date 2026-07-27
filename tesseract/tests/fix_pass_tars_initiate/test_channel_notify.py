"""channel_notify — TARS-initiated outbound text on an external channel."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tesseract.integrations import clear_registry, register_channel
from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.channel_notify import (
    MAX_TEXT_CHARS,
    ChannelNotifyInput,
    ChannelNotifyTool,
)


class _FakeAdapter:
    """Minimal ChannelAdapter shape — enough to satisfy registry checks
    plus the send_text / allowlist surface channel_notify uses.
    """

    name = "telegram"

    def __init__(self, *, allowlist: Any = None, user_tier: dict | None = None) -> None:
        self.allowlist = allowlist
        self.user_tier = user_tier or {}
        self.sent: list[dict[str, Any]] = []

    async def send_text(self, *, chat_ref, text, reply_to_message_id=None) -> None:
        self.sent.append({
            "chat_ref": chat_ref, "text": text,
            "reply_to_message_id": reply_to_message_id,
        })

    # ChannelAdapter protocol satisfiers — never called in these tests
    # but required so register_channel's runtime-checkable accepts us.
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def status_snapshot(self): return None
    async def list_users(self): return []
    async def list_threads(self, **_): return []
    async def fetch_history(self, **_): return []
    async def send_reply(self, **_): return None
    async def approve_pending(self, **_): return None
    async def block_user(self, **_): return None
    async def unblock_user(self, **_): return None
    async def set_user_tier(self, **_): return None
    async def post_admin_message(self, **_): return None
    async def resolve_chat_ref(self, **_): return None
    async def react_to_message(self, **_): return None
    async def send_voice(self, **_): return None
    async def send_photo(self, **_): return None
    async def send_document(self, **_): return None


@pytest.fixture(autouse=True)
def _isolate_channel_registry():
    clear_registry()
    yield
    clear_registry()


@pytest.mark.asyncio
async def test_channel_notify_targeted_chat() -> None:
    adapter = _FakeAdapter()
    # Bypass register_channel's protocol check — _FakeAdapter implements
    # the surface we exercise but not every protocol member.
    from tesseract.integrations import _CHANNEL_REGISTRY
    _CHANNEL_REGISTRY["telegram"] = adapter

    tool = ChannelNotifyTool()
    result = await tool.run(
        ChannelNotifyInput(text="hello from TARS", chat_ref="42"),
        ToolContext(),
    )

    assert not result.is_error
    assert adapter.sent == [{
        "chat_ref": "42", "text": "hello from TARS",
        "reply_to_message_id": None,
    }]
    assert "42" in result.output


@pytest.mark.asyncio
async def test_channel_notify_unregistered_channel() -> None:
    tool = ChannelNotifyTool()
    result = await tool.run(
        ChannelNotifyInput(text="ping", chat_ref="42", channel="signal"),
        ToolContext(),
    )
    assert result.is_error
    assert "not registered" in result.output


@pytest.mark.asyncio
async def test_channel_notify_empty_text_rejected() -> None:
    tool = ChannelNotifyTool()
    result = await tool.run(
        ChannelNotifyInput(text="   ", chat_ref="42"),
        ToolContext(),
    )
    assert result.is_error
    assert "empty" in result.output


@pytest.mark.asyncio
async def test_channel_notify_text_cap_enforced() -> None:
    tool = ChannelNotifyTool()
    result = await tool.run(
        ChannelNotifyInput(text="x" * (MAX_TEXT_CHARS + 1), chat_ref="42"),
        ToolContext(),
    )
    assert result.is_error
    assert "cap is" in result.output


@pytest.mark.asyncio
async def test_channel_notify_fan_to_operators() -> None:
    allowlist = SimpleNamespace(chat_ids={"111", "222"}, blocked=set(), pending={})
    user_tier = {"111": "operator", "222": "operator"}
    adapter = _FakeAdapter(allowlist=allowlist, user_tier=user_tier)
    from tesseract.integrations import _CHANNEL_REGISTRY
    _CHANNEL_REGISTRY["telegram"] = adapter

    tool = ChannelNotifyTool()
    result = await tool.run(
        ChannelNotifyInput(text="broadcast"),
        ToolContext(),
    )

    assert not result.is_error
    assert len(adapter.sent) == 2
    assert result.metadata == {"sent": 2, "skipped": 0, "errors": 0}


@pytest.mark.asyncio
async def test_channel_notify_falls_back_to_state_allowlist() -> None:
    """Production `TelegramBridge` keeps allowlist/user_tier on
    `self._state`, not as top-level attributes. The fan-to-operators
    path must walk into `_state` so a real bridge can be the target.
    Regression for review finding (channel_notify rev 1).
    """
    allowlist = SimpleNamespace(chat_ids={"111"}, blocked=set(), pending={})
    poll_state = SimpleNamespace(user_tier={"111": "operator"})
    adapter = _FakeAdapter()
    # Mimic real bridge: surface only via _state.allowlist / _state.poll_state.
    adapter.allowlist = None
    adapter.user_tier = None
    adapter._state = SimpleNamespace(allowlist=allowlist, poll_state=poll_state)
    from tesseract.integrations import _CHANNEL_REGISTRY
    _CHANNEL_REGISTRY["telegram"] = adapter

    tool = ChannelNotifyTool()
    result = await tool.run(
        ChannelNotifyInput(text="alert"),
        ToolContext(),
    )

    assert not result.is_error
    assert adapter.sent == [{
        "chat_ref": "111", "text": "alert", "reply_to_message_id": None,
    }]
