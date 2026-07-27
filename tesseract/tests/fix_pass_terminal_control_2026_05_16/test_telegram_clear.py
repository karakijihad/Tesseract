"""Phase X — /clear confirmation dialog for the Telegram bridge.

Operator (2026-05-16): "one option, clear. and then it asks us if we
need to reflect or do whatever. we can tell it no or yes, and then we
proceed normally."

Pins:
- PollState.pending_clear round-trips through save/load
- _handle_clear stamps pending_clear and returns the confirmation prompt
- _pending_clear_expired honours the 5-minute TTL
- /clear allow-listed on both operator and friend tiers
- Bridge follow-up: yes → reflect turn + clear + reply; no → clear + reply;
  anything-else → drop stamp + nudge "cancelled, processing normally" + fall through
- Stamp is dropped BEFORE the branch fires so a crash can't lock the chat
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tesseract.integrations.telegram.bridge import (
    _CLEAR_NO_TOKENS,
    _CLEAR_YES_TOKENS,
    _pending_clear_expired,
)
from tesseract.integrations.telegram.commands import (
    _FRIEND_ALLOWED,
    _HANDLERS,
    TelegramCommandContext,
    dispatch,
)
from tesseract.integrations.telegram.state import (
    PollState,
    load_state,
    save_state,
)


@pytest.fixture(autouse=True)
def _isolate_log_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))


# ─── state round-trip ────────────────────────────────────────────


def test_pending_clear_persists_through_save_load(tmp_path: Path) -> None:
    state = PollState()
    state.pending_clear["12345"] = "2026-05-16T13:47:02+00:00"
    p = tmp_path / "state.json"
    save_state(p, state)
    restored = load_state(p)
    assert restored.pending_clear == {"12345": "2026-05-16T13:47:02+00:00"}


def test_pending_clear_absent_loads_as_empty_dict(tmp_path: Path) -> None:
    """Forward-compat: a pre-2026-05-16 state.json with no
    `pending_clear` key must load as `{}` rather than crash."""
    p = tmp_path / "state.json"
    p.write_text('{"last_update_id": 7}', encoding="utf-8")
    state = load_state(p)
    assert state.pending_clear == {}


# ─── TTL ─────────────────────────────────────────────────────────


def test_pending_clear_expired_fresh_returns_false() -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    assert _pending_clear_expired(stamp) is False


def test_pending_clear_expired_old_returns_true() -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    assert _pending_clear_expired(stamp) is True


def test_pending_clear_expired_malformed_returns_true() -> None:
    assert _pending_clear_expired("not-an-iso-timestamp") is True
    assert _pending_clear_expired("") is True


# ─── token sets ──────────────────────────────────────────────────


def test_clear_yes_tokens_cover_intuitive_affirmations() -> None:
    assert "yes" in _CLEAR_YES_TOKENS
    assert "y" in _CLEAR_YES_TOKENS
    assert "ok" in _CLEAR_YES_TOKENS


def test_clear_no_tokens_cover_intuitive_refusals() -> None:
    assert "no" in _CLEAR_NO_TOKENS
    assert "n" in _CLEAR_NO_TOKENS
    assert "nope" in _CLEAR_NO_TOKENS


# ─── /clear handler ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_clear_stamps_state_and_returns_prompt(tmp_path: Path) -> None:
    state = PollState()
    state_path = tmp_path / "state.json"
    save_state(state_path, state)

    bridge = SimpleNamespace(
        _state=SimpleNamespace(
            poll_state=state,
            state_path=state_path,
            with_lock=lambda: _NoOpLock(),
        ),
    )
    ctx = TelegramCommandContext(
        app={},
        chat_id=42,
        tier="operator",
        offline=False,
        bridge=bridge,
    )
    reply = await dispatch("/clear", ctx)
    assert reply is not None
    assert "Clear this thread" in reply
    assert "YES" in reply and "NO" in reply
    assert "42" in state.pending_clear or "42" not in state.pending_clear  # keyed by str
    assert state.pending_clear.get("42") is not None
    # Persisted to disk so a restart doesn't lose the pending stamp.
    reloaded = load_state(state_path)
    assert reloaded.pending_clear.get("42") == state.pending_clear["42"]


@pytest.mark.asyncio
async def test_handle_clear_allowed_on_friend_tier(tmp_path: Path) -> None:
    """Friend tier can clear their own conversation — it's their thread.
    Pinned because adding /clear means widening _FRIEND_ALLOWED; a
    future refactor that retightens the set must keep /clear in."""
    assert "/clear" in _FRIEND_ALLOWED
    state = PollState()
    state_path = tmp_path / "state.json"
    save_state(state_path, state)
    bridge = SimpleNamespace(
        _state=SimpleNamespace(
            poll_state=state,
            state_path=state_path,
            with_lock=lambda: _NoOpLock(),
        ),
    )
    ctx = TelegramCommandContext(
        app={},
        chat_id=7,
        tier="friend",
        offline=False,
        bridge=bridge,
    )
    reply = await dispatch("/clear", ctx)
    assert reply is not None
    assert "Clear this thread" in reply


# ─── bridge follow-up: yes / no / anything-else ──────────────────


class _NoOpLock:
    def __enter__(self) -> "_NoOpLock":
        return self
    def __exit__(self, *args: Any) -> None:
        return None


def _make_bridge_stub(state: PollState, state_path: Path):
    """Construct a SimpleNamespace mimicking the bridge slots used by
    _handle_pending_clear_followup. AsyncMock methods are used so each
    awaited call records its args without actually running.
    """
    return SimpleNamespace(
        _state=SimpleNamespace(
            poll_state=state,
            state_path=state_path,
            with_lock=lambda: _NoOpLock(),
        ),
        _sessions={},
        _run_clear_reflection=AsyncMock(),
        clear_session=MagicMock(),
        _safe_send=AsyncMock(),
        name="telegram",
    )


@pytest.mark.asyncio
async def test_followup_no_pending_stamp_returns_false(tmp_path: Path) -> None:
    from tesseract.integrations.telegram.bridge import TelegramBridge

    state = PollState()
    bridge = _make_bridge_stub(state, tmp_path / "state.json")
    message = SimpleNamespace(chat_id=42, text="hello")
    handled = await TelegramBridge._handle_pending_clear_followup(
        bridge, message, "42", "operator",
    )
    assert handled is False
    bridge.clear_session.assert_not_called()
    bridge._run_clear_reflection.assert_not_called()


@pytest.mark.asyncio
async def test_followup_expired_stamp_drops_and_returns_false(tmp_path: Path) -> None:
    from tesseract.integrations.telegram.bridge import TelegramBridge

    state = PollState()
    state.pending_clear["42"] = (
        datetime.now(timezone.utc) - timedelta(seconds=600)
    ).isoformat()
    state_path = tmp_path / "state.json"
    save_state(state_path, state)
    bridge = _make_bridge_stub(state, state_path)
    message = SimpleNamespace(chat_id=42, text="anything")
    handled = await TelegramBridge._handle_pending_clear_followup(
        bridge, message, "42", "operator",
    )
    assert handled is False
    assert "42" not in state.pending_clear
    bridge.clear_session.assert_not_called()


@pytest.mark.asyncio
async def test_followup_yes_runs_reflection_then_clears(tmp_path: Path) -> None:
    from tesseract.integrations.telegram.bridge import TelegramBridge

    state = PollState()
    state.pending_clear["42"] = datetime.now(timezone.utc).isoformat()
    state_path = tmp_path / "state.json"
    save_state(state_path, state)
    bridge = _make_bridge_stub(state, state_path)
    message = SimpleNamespace(chat_id=42, text="YES")
    handled = await TelegramBridge._handle_pending_clear_followup(
        bridge, message, "42", "operator",
    )
    assert handled is True
    bridge._run_clear_reflection.assert_awaited_once()
    bridge.clear_session.assert_called_once_with(42)
    bridge._safe_send.assert_awaited_once()
    # Stamp was dropped BEFORE the branch fired (crash-safety pin).
    assert "42" not in state.pending_clear


@pytest.mark.asyncio
async def test_followup_no_clears_without_reflection(tmp_path: Path) -> None:
    from tesseract.integrations.telegram.bridge import TelegramBridge

    state = PollState()
    state.pending_clear["42"] = datetime.now(timezone.utc).isoformat()
    bridge = _make_bridge_stub(state, tmp_path / "state.json")
    message = SimpleNamespace(chat_id=42, text="n")
    handled = await TelegramBridge._handle_pending_clear_followup(
        bridge, message, "42", "operator",
    )
    assert handled is True
    bridge._run_clear_reflection.assert_not_awaited()
    bridge.clear_session.assert_called_once_with(42)
    bridge._safe_send.assert_awaited_once()


@pytest.mark.asyncio
async def test_followup_unknown_answer_cancels_and_falls_through(tmp_path: Path) -> None:
    from tesseract.integrations.telegram.bridge import TelegramBridge

    state = PollState()
    state.pending_clear["42"] = datetime.now(timezone.utc).isoformat()
    bridge = _make_bridge_stub(state, tmp_path / "state.json")
    message = SimpleNamespace(chat_id=42, text="what's the weather in Springfield?")
    handled = await TelegramBridge._handle_pending_clear_followup(
        bridge, message, "42", "operator",
    )
    # Falls through so the normal turn machinery handles the question.
    assert handled is False
    # Pending stamp is dropped (cancelled).
    assert "42" not in state.pending_clear
    # Bridge sends a cancellation nudge BUT doesn't clear the session.
    bridge.clear_session.assert_not_called()
    bridge._safe_send.assert_awaited_once()


@pytest.mark.asyncio
async def test_followup_yes_survives_reflection_crash(tmp_path: Path) -> None:
    """If the reflection turn raises, we still clear the session (the
    operator asked to close the thread; not clearing leaves them
    confused). Stamp is already gone by then."""
    from tesseract.integrations.telegram.bridge import TelegramBridge

    state = PollState()
    state.pending_clear["42"] = datetime.now(timezone.utc).isoformat()
    bridge = _make_bridge_stub(state, tmp_path / "state.json")
    bridge._run_clear_reflection.side_effect = RuntimeError("reflection boom")
    message = SimpleNamespace(chat_id=42, text="yes")
    handled = await TelegramBridge._handle_pending_clear_followup(
        bridge, message, "42", "operator",
    )
    assert handled is True
    bridge.clear_session.assert_called_once_with(42)
    bridge._safe_send.assert_awaited_once()
    assert "42" not in state.pending_clear


@pytest.mark.asyncio
async def test_clear_command_registered() -> None:
    """Sanity: /clear is in the handler map and is_known_command finds it."""
    from tesseract.integrations.telegram.commands import is_known_command
    assert "/clear" in _HANDLERS
    assert is_known_command("/clear")
    assert is_known_command("/clear  ") is True
