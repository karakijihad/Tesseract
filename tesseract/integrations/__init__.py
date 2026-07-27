"""External integration bridges.

Channels (Telegram first; WhatsApp / Signal / Discord later) implement
the :class:`ChannelAdapter` protocol from :mod:`._channel_adapter`. This
module exposes a process-wide registry the Mirror Channels tab queries.

The registry is keyed by ``adapter.name`` (lowercase channel slug). Mirror
``app.py`` populates it during startup after each ``build_*_bridge``
factory succeeds; tests register fakes directly.
"""

from __future__ import annotations

from tesseract.integrations._channel_adapter import (
    ChannelAdapter,
    ChannelBridgeState,
    ChannelMessage,
    ChannelStatus,
    ChannelUser,
    ChannelUserState,
    ChannelUserTier,
)

__all__ = [
    "ChannelAdapter",
    "ChannelBridgeState",
    "ChannelMessage",
    "ChannelStatus",
    "ChannelUser",
    "ChannelUserState",
    "ChannelUserTier",
    "register_channel",
    "unregister_channel",
    "get_channel",
    "list_channels",
    "clear_registry",
]


_CHANNEL_REGISTRY: dict[str, ChannelAdapter] = {}


def register_channel(adapter: ChannelAdapter) -> None:
    """Register ``adapter`` under its ``name`` attribute.

    Idempotent: re-registering the same name overwrites the prior entry
    (Mirror restart, hot reload). Raises ``TypeError`` when the candidate
    does not satisfy the runtime-checkable protocol — catches a refactor
    that drops a required method.
    """
    if not isinstance(adapter, ChannelAdapter):
        raise TypeError(
            f"register_channel: {type(adapter).__name__} does not satisfy ChannelAdapter"
        )
    name = getattr(adapter, "name", "")
    if not isinstance(name, str) or not name:
        raise ValueError("register_channel: adapter.name must be a non-empty string")
    _CHANNEL_REGISTRY[name] = adapter


def unregister_channel(name: str) -> None:
    _CHANNEL_REGISTRY.pop(name, None)


def get_channel(name: str) -> ChannelAdapter | None:
    return _CHANNEL_REGISTRY.get(name)


def list_channels() -> list[ChannelAdapter]:
    return list(_CHANNEL_REGISTRY.values())


def clear_registry() -> None:
    """Test-helper: drop every registered adapter."""
    _CHANNEL_REGISTRY.clear()
