"""``ChannelAdapter`` protocol — abstraction every external-channel bridge implements.

Telegram is the first concrete adapter (MO-9-10); WhatsApp / Signal / Discord
land later by implementing the same protocol. The Mirror Channels tab
(MO-9-11 / 12) talks to this protocol, never to a concrete adapter directly.

Seeded contract: ``Docs/Plan/mission-orchestrator/MO-9/_shared/channel-adapter-protocol.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

ChannelUserTier = Literal["operator", "friend"]
ChannelUserState = Literal["allowed", "pending", "blocked"]
ChannelBridgeState = Literal["running", "stopped", "error"]


@dataclass(frozen=True)
class ChannelUser:
    user_id: str
    display_name: str
    tier: ChannelUserTier
    ttl_iso: str | None
    first_seen: str
    last_seen: str
    messages_total: int
    state: ChannelUserState


@dataclass(frozen=True)
class ChannelStatus:
    name: str
    bridge_state: ChannelBridgeState
    last_poll_at: str | None
    error_count_24h: int
    messages_in_24h: int
    messages_out_24h: int
    pending_count: int
    allowed_count: int


@dataclass(frozen=True)
class ChannelMessage:
    """One row in ``logs/channels/<channel>/<chat_id>/conversations.jsonl``.

    ``extra`` carries channel-native metadata (Telegram message_id, etc.).
    ``attachments`` holds the typed envelopes produced by the bridge (CR-2); old rows default to ``()``.
    """

    ts: str
    direction: Literal["inbound", "outbound"]
    body: str
    extra: dict[str, Any]
    attachments: tuple[Any, ...] = field(default_factory=tuple)


@runtime_checkable
class ChannelAdapter(Protocol):
    """Protocol every channel bridge implements.

    `runtime_checkable` so the Channels REST layer can `isinstance(bridge, ChannelAdapter)`
    when iterating the registry.
    """

    name: str

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def status_snapshot(self) -> ChannelStatus: ...

    def list_users(self) -> list[ChannelUser]: ...

    async def approve(
        self,
        user_id: str,
        *,
        tier: ChannelUserTier,
        ttl_iso: str | None,
        display_name: str | None,
    ) -> ChannelUser: ...

    async def revoke(self, user_id: str) -> ChannelUser: ...

    async def block(self, user_id: str) -> ChannelUser: ...

    def list_conversation(
        self,
        user_id: str,
        *,
        limit: int = 100,
        before_iso: str | None = None,
    ) -> list[dict[str, Any]]: ...
