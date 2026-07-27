"""Session 3 (2026-05-16) — /voice_on /voice_off slash commands + voice-reply path."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tesseract.integrations.telegram.commands import (
    TelegramCommandContext,
    dispatch,
    is_known_command,
)
from tesseract.integrations.telegram.state import StateBundle, load_state


def _ctx(tmp_path: Path, *, tier: str = "operator") -> tuple[TelegramCommandContext, object]:
    state_dir = tmp_path / "telegram"
    state_dir.mkdir(parents=True, exist_ok=True)
    bundle = StateBundle(dir_path=state_dir)
    bridge = MagicMock()
    bridge._state = bundle
    ctx = TelegramCommandContext(
        app={}, chat_id=99, tier=tier, offline=False, bridge=bridge,
    )
    return ctx, bundle


@pytest.mark.asyncio
async def test_voice_on_flips_state_and_persists(tmp_path) -> None:
    ctx, bundle = _ctx(tmp_path)
    assert is_known_command("/voice_on")
    reply = await dispatch("/voice_on", ctx)
    assert reply is not None and "on" in reply.lower()
    # In-memory state flipped.
    assert bundle.poll_state.reply_voice["99"] is True
    # Persisted to disk.
    on_disk = load_state(bundle.state_path)
    assert on_disk.reply_voice["99"] is True


@pytest.mark.asyncio
async def test_voice_off_drops_state(tmp_path) -> None:
    ctx, bundle = _ctx(tmp_path)
    await dispatch("/voice_on", ctx)
    await dispatch("/voice_off", ctx)
    assert "99" not in bundle.poll_state.reply_voice
    on_disk = load_state(bundle.state_path)
    assert "99" not in on_disk.reply_voice


@pytest.mark.asyncio
async def test_voice_commands_friend_tier_denied(tmp_path) -> None:
    ctx, _ = _ctx(tmp_path, tier="friend")
    reply = await dispatch("/voice_on", ctx)
    # The dispatcher returns the standard "operator-only" deny string.
    assert reply is not None and "operator" in reply.lower()
