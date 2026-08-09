"""MCP session registry — the ``Mcp-Session-Id`` ↔ ``mcp_session`` Activity
record binding (mcp-control-plane P4).

A real MCP client opens a session with ``initialize`` and (per the Streamable
HTTP transport) carries the server-assigned ``Mcp-Session-Id`` on every
subsequent request. That session is the "who's in the chair" unit — one
top-level ``mcp_session`` ActivityRecord per connection, killable via
``activity.cancel``. The session id IS the activity id (opaque
``mcp:<client>:<hex>``) so ``activity.cancel`` maps a record id straight back
to the session with no second lookup table.

Session ids carry 128 bits of CSPRNG output (``secrets.token_hex(16)``) — the id
is a bearer capability (a leaked id lets another process resume/cancel the
session), so it must not be truncated below capability-grade entropy.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from tesseract.config.mcp import MCPClient
from tesseract.orchestrator.activity import get_activity_registry
from tesseract.orchestrator.activity.models import ActivityRecord


@dataclass(frozen=True)
class MCPSession:
    session_id: str  # == the mcp_session activity_id
    client: MCPClient
    protocol_version: str


class MCPSessionRegistry:
    """In-memory map of live MCP sessions. One per Mirror app, held on the
    ``MCPServer``. Bounded by ``max_connections`` at the open site.

    Tracks last-activity per session (registry-side, since ``MCPSession`` is
    frozen) to support the idle sweep (``sweep_idle``): a client that vanishes
    without ``DELETE /mcp`` would otherwise leave a zombie session + a
    forever-"running" ``mcp_session`` Activity record."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        on_close: Callable[[str], None] | None = None,
    ) -> None:
        self._sessions: dict[str, MCPSession] = {}
        self._last_seen: dict[str, float] = {}
        self._clock = clock
        # Fires on every close path — DELETE, the idle sweep, activity.cancel,
        # shutdown — so anything bound to a session (the SSE stream) is torn
        # down by the same act that ends it, rather than by each caller
        # remembering to.
        self._on_close = on_close

    def __len__(self) -> int:
        return len(self._sessions)

    def open(self, client: MCPClient, protocol_version: str) -> MCPSession:
        """Mint a session id, register its ``mcp_session`` Activity record, and
        return the session. The record is ``ephemeral`` (swept on close, not a
        persisted work item)."""
        session_id = f"mcp:{client.name}:{secrets.token_hex(16)}"
        get_activity_registry().register(
            ActivityRecord(
                activity_id=session_id,
                kind="mcp_session",
                label=f"MCP · {client.name} ({client.trust_tier})",
                state="running",
                durability="ephemeral",
                # Without this the client cannot see — or cancel — its own
                # session once `activity.list` is caller-scoped: a blank owner
                # reads as the runtime's own work.
                owner_principal=client.name,
            )
        )
        session = MCPSession(
            session_id=session_id, client=client, protocol_version=protocol_version
        )
        self._sessions[session_id] = session
        self._last_seen[session_id] = self._clock()
        return session

    def get(self, session_id: str | None) -> MCPSession | None:
        if not session_id:
            return None
        return self._sessions.get(session_id)

    def touch(self, session_id: str) -> None:
        """Record activity on a live session. No-op for an unknown id."""
        if session_id in self._sessions:
            self._last_seen[session_id] = self._clock()

    def close(self, session_id: str) -> bool:
        """Terminate a session: drop it and mark its Activity record closed.
        Returns False for an unknown/already-closed id (idempotent)."""
        session = self._sessions.pop(session_id, None)
        self._last_seen.pop(session_id, None)
        if session is None:
            return False
        if self._on_close is not None:
            self._on_close(session_id)
        get_activity_registry().update_state(session_id, "closed")
        return True

    def close_all(self) -> None:
        for session_id in list(self._sessions):
            self.close(session_id)

    def sweep_idle(self, idle_timeout_s: float) -> list[str]:
        """Close every session whose last activity is older than
        ``idle_timeout_s``. Returns the swept session ids."""
        now = self._clock()
        stale = [
            session_id
            for session_id, last_seen in list(self._last_seen.items())
            if now - last_seen > idle_timeout_s
        ]
        for session_id in stale:
            self.close(session_id)
        return stale


__all__ = ["MCPSession", "MCPSessionRegistry"]
