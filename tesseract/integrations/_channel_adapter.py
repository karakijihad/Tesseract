"""``ChannelAdapter`` protocol — abstraction every external-channel bridge implements.

Telegram is the first concrete adapter; WhatsApp / Signal / Discord
land later by implementing the same protocol. The Mirror Channels tab
talks to this protocol, never to a concrete adapter directly.

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
    ``attachments`` holds the typed envelopes produced by the bridge; old rows default to ``()``.
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

    # `send_text(*, chat_ref, text, disable_web_page_preview=False)` is the
    # other half of an adapter and every one of them has it, but it is
    # deliberately not declared here: this protocol is `runtime_checkable` and
    # `register_channel` refuses anything that misses a member, so adding it
    # would turn an incomplete adapter into a boot failure rather than a
    # failure at the moment something is sent. `_outbound.notify_operators`
    # asks for it when it needs it and reports a channel that cannot speak.
