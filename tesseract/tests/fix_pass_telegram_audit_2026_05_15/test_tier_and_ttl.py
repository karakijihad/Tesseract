"""Audit fix M3 — tier denylist + TTL auto-revoke tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from tesseract.integrations._channel_tier import (
    FRIEND_DENIED_TOOLS,
    build_tiered_ask_fn,
    is_friend_denied,
)
from tesseract.integrations._retention import RetentionPolicy
from tesseract.integrations.telegram.api import TelegramMessage
from tesseract.integrations.telegram.bridge import TelegramBridge
from tesseract.integrations.telegram.state import StateBundle, save_allowlist, save_state


def test_friend_denied_known_names_match_audit_spec() -> None:
    # Audit M3 names mission/terminal/delegation/source-edit/promotion.
    for name in (
        "mission_loop_create",
        "process_start",
        "bash",
        "delegate_claude",
        "agent_create",
        "agent_promote",
        "file_write",
    ):
        assert is_friend_denied(name), f"{name} should be denied for friend tier"


def test_friend_denied_prefix_catches_future_tools() -> None:
    assert is_friend_denied("mission_new_thing")
    assert is_friend_denied("delegate_future")
    assert is_friend_denied("agent_anything")
    assert is_friend_denied("pty_inspect")


def test_friend_allowed_safe_tools() -> None:
    for name in ("memory_search", "vault_search", "web_search", "tavily_extract"):
        assert not is_friend_denied(name), f"{name} should be allowed for friend tier"


@pytest.mark.asyncio
async def test_tiered_ask_fn_denies_friend_blacklist_without_calling_inner() -> None:
    inner = AsyncMock(return_value=True)
    wrapper = build_tiered_ask_fn(
        tier="friend", inner=inner, channel="telegram", chat_id="1"
    )
    tool = MagicMock()
    tool.name = "mission_loop_create"
    result = await wrapper(tool, MagicMock(), MagicMock())
    assert result is False
    inner.assert_not_called()


@pytest.mark.asyncio
async def test_tiered_ask_fn_passes_through_for_operator() -> None:
    inner = AsyncMock(return_value=False)
    wrapper = build_tiered_ask_fn(
        tier="operator", inner=inner, channel="telegram", chat_id="1"
    )
    # Operator-tier is a pure pass-through — the wrapper is the inner.
    assert wrapper is inner


@pytest.mark.asyncio
async def test_tiered_ask_fn_passes_safe_tools_for_friend() -> None:
    inner = AsyncMock(return_value=True)
    wrapper = build_tiered_ask_fn(
        tier="friend", inner=inner, channel="telegram", chat_id="1"
    )
    tool = MagicMock()
    tool.name = "memory_search"
    result = await wrapper(tool, MagicMock(), MagicMock())
    assert result is True
    inner.assert_awaited_once()


def _bridge(tmp_path, monkeypatch, *, chat: int, ttl_iso: str | None) -> TelegramBridge:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge._api = MagicMock()
    bridge._api.send_message = AsyncMock(return_value={"message_id": 1})
    bridge._api.send_chat_action = AsyncMock()
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
    bridge._state.allowlist.chat_ids.add(chat)
    save_allowlist(bridge._state.allowlist_path, bridge._state.allowlist)
    if ttl_iso is not None:
        bridge._state.poll_state.user_ttl[str(chat)] = ttl_iso
        save_state(bridge._state.state_path, bridge._state.poll_state)
    return bridge


@pytest.mark.asyncio
async def test_expired_ttl_auto_revokes_and_notifies(tmp_path, monkeypatch) -> None:
    expired = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    bridge = _bridge(tmp_path, monkeypatch, chat=444, ttl_iso=expired)

    msg = TelegramMessage(
        update_id=1,
        message_id=1,
        chat_id=444,
        chat_type="private",
        from_user_id=1,
        from_username="x",
        text="hi",
        date=0,
    )
    await bridge._handle_message(msg)

    # chat moved out of allowlist, into pending.
    assert 444 not in bridge._state.allowlist.chat_ids
    assert 444 in bridge._state.allowlist.pending
    # TTL cleared from state.
    assert "444" not in bridge._state.poll_state.user_ttl
    # User got the "access expired" message.
    sent_text = bridge._api.send_message.call_args.kwargs["text"]
    assert "expired" in sent_text.lower()


@pytest.mark.asyncio
async def test_future_ttl_lets_message_through(tmp_path, monkeypatch) -> None:
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    bridge = _bridge(tmp_path, monkeypatch, chat=555, ttl_iso=future)
    # Stub the turn dispatch so the test stays in-bridge.
    from tesseract.integrations.telegram import bridge as bridge_mod

    bridge_mod._start_channel_turn = AsyncMock(return_value="ok")  # type: ignore[attr-defined]
    bridge._session_for = MagicMock(return_value=MagicMock(current_turn_task=None))
    bridge._send_thinking_placeholder = AsyncMock(return_value=None)
    bridge._build_progress_callback = MagicMock(
        return_value=MagicMock(_throttler=MagicMock(stop=AsyncMock()))
    )
    bridge._send_outbound = AsyncMock()

    msg = TelegramMessage(
        update_id=1,
        message_id=1,
        chat_id=555,
        chat_type="private",
        from_user_id=1,
        from_username="x",
        text="hi",
        date=0,
    )
    await bridge._handle_message(msg)

    # Chat still allowlisted.
    assert 555 in bridge._state.allowlist.chat_ids


def test_friend_denylist_includes_brief_render() -> None:
    """A3 — natural-language `/brief`-style render is operator-only."""
    assert "brief_render" in FRIEND_DENIED_TOOLS


def test_friend_denylist_covers_workspace_writers() -> None:
    """Reviewer P1 — propose_change + soul_growth_propose are
    ``default_posture="auto"`` and must not be reachable from a
    friend-tier chat. ``propose_`` prefix catches future variants.
    """
    assert "propose_change" in FRIEND_DENIED_TOOLS
    assert "soul_growth_propose" in FRIEND_DENIED_TOOLS
    assert is_friend_denied("propose_anything_new")


def test_friend_denylist_includes_brief_read() -> None:
    """Reviewer P1 — `brief_read` exposes daily brief content; friend
    tier must not call it via chat-side natural language."""
    assert "brief_read" in FRIEND_DENIED_TOOLS
    assert is_friend_denied("brief_read")
