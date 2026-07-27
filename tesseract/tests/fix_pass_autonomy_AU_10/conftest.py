"""Shared fixtures for AU-10 OutboundNotifier + quick-reply tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Route ``TESSERACT_HOME`` writes to ``tmp_path`` BEFORE any module
    that resolves ``outbound_rates_path()`` / ``outbound_mutes_path()``
    runs. Mirrors the AU-6 fixture pattern."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


class FakeAllowlist:
    def __init__(self, chat_ids: set[int] | None = None) -> None:
        self.chat_ids = chat_ids or {12345, 67890}
        self.blocked: set[int] = set()
        self.pending: dict[int, Any] = {}


class FakePollState:
    def __init__(self, user_tier: dict[str, str] | None = None) -> None:
        self.user_tier = user_tier or {"12345": "operator", "67890": "operator"}


class FakeBridgeState:
    def __init__(self) -> None:
        self.allowlist = FakeAllowlist()
        self.poll_state = FakePollState()


class FakeBridge:
    """Stand-in for the live Telegram bridge with the surface the
    notifier touches: ``_state.allowlist`` + ``_state.poll_state.user_tier``
    and an awaitable ``send_text``."""

    def __init__(self) -> None:
        self._state = FakeBridgeState()
        self.sent: list[dict[str, Any]] = []

    async def send_text(self, *, chat_ref: str, text: str) -> None:  # noqa: D401
        self.sent.append({"chat_ref": chat_ref, "text": text})


class FakeChannelsConfig:
    """Minimal shape the notifier reads:

    * ``channel_block(name)`` returns an object with ``outbound_rate``
      (``default_per_hour``, ``per_category``) + ``muted_categories``.
    """

    class _Telegram:
        def __init__(
            self,
            *,
            default_per_hour: int = 6,
            per_category: dict[str, int] | None = None,
            muted_categories: list[str] | None = None,
        ) -> None:
            class _Rate:
                def __init__(self) -> None:
                    self.default_per_hour = default_per_hour
                    self.per_category = per_category or {}

            self.outbound_rate = _Rate()
            self.muted_categories = muted_categories or []
            self.enabled = True

    def __init__(
        self,
        *,
        default_per_hour: int = 6,
        per_category: dict[str, int] | None = None,
        muted_categories: list[str] | None = None,
    ) -> None:
        self.telegram = self._Telegram(
            default_per_hour=default_per_hour,
            per_category=per_category,
            muted_categories=muted_categories,
        )

    def channel_block(self, name: str) -> Any | None:
        if name == "telegram":
            return self.telegram
        return None


@pytest.fixture
def bridge() -> FakeBridge:
    return FakeBridge()


@pytest.fixture
def channels_config() -> FakeChannelsConfig:
    return FakeChannelsConfig()


@pytest.fixture
def fixed_clock():
    """Returns a tuple ``(clock_fn, advance_fn)``. Tests advance the
    clock by seconds to exercise the sliding-window prune."""
    now = [datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)]

    def clock() -> datetime:
        return now[0]

    def advance(seconds: float) -> None:
        from datetime import timedelta as _td

        now[0] = now[0] + _td(seconds=seconds)

    return clock, advance


__all__ = [
    "FakeBridge",
    "FakeChannelsConfig",
    "bridge",
    "channels_config",
    "fixed_clock",
    "isolated_home",
]
