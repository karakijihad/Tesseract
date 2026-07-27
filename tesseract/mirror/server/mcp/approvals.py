"""ASK-over-MCP operator approvals (P3 session 3).

An ASK-posture verb needs operator approval before it runs. The dispatcher's
``verb_ask_fn`` registers a pending approval, surfaces it to the operator, and
awaits the decision — bounded by ``mcp.yaml::server.ask_hold_timeout_s``. If the
operator approves in time the verb executes (HTTP held open); if they decline it
returns 403.

**Timeout is terminal for that call.** This is a bounded-hold model: once the
window elapses there is no held request left to resume, so the pending approval
is discarded and the held request degrades to the async ``awaiting_operator``
handle (HTTP 202). The ``approval_id`` on that handle is a *correlation* token
(it matches the ``mcp_approval_requested`` event + the audit row) — it is NOT
resolvable after timeout: a late ``POST /decision`` on it returns 404. To retry,
the client re-issues the verb; ongoing WORK is observed via ``activity.watch``,
not the expired approval (Doclog 2026-07-01 §ASK-over-MCP).

The operator resolves approvals through the operator-facing REST surface
(``routes/mcp_approvals.py``) — NOT the MCP bearer path; the approver is the
local operator, not the remote client.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from tesseract.config.mcp import MCPClient

log = logging.getLogger(__name__)

# Called to surface a new approval request to the operator (verb + client).
ApprovalEmit = Callable[[str, str, MCPClient], None]


class MCPApprovalTimeout(Exception):
    """Raised by the ask_fn when the operator did not decide within the hold
    window. Carries the ``approval_id`` so the dispatcher can return a handle
    that keeps referencing the same pending approval."""

    def __init__(self, approval_id: str) -> None:
        super().__init__(approval_id)
        self.approval_id = approval_id


@dataclass
class _Pending:
    approval_id: str
    verb: str
    client: str
    future: "asyncio.Future[bool]"


class MCPApprovalRegistry:
    """In-memory map of approval_id → pending future. Single Mirror app, single
    event loop — no locking needed (all access is on the loop thread)."""

    def __init__(self) -> None:
        self._pending: dict[str, _Pending] = {}

    def create(self, approval_id: str, verb: str, client: str) -> "asyncio.Future[bool]":
        future: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        self._pending[approval_id] = _Pending(approval_id, verb, client, future)
        return future

    def resolve(self, approval_id: str, approved: bool) -> bool:
        """Operator decision. Returns False if the id is unknown or already
        settled (expired/decided) — the route surfaces that as 404."""
        pending = self._pending.pop(approval_id, None)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(approved)
        return True

    def discard(self, approval_id: str) -> None:
        self._pending.pop(approval_id, None)

    def pending(self) -> list[dict[str, str]]:
        return [
            {"approval_id": p.approval_id, "verb": p.verb, "client": p.client}
            for p in self._pending.values()
        ]


def build_verb_ask_fn(
    registry: MCPApprovalRegistry,
    emit: ApprovalEmit,
    timeout_s: float,
) -> Callable[[str, dict[str, Any], MCPClient], Awaitable[bool]]:
    """Build the dispatcher's ``verb_ask_fn``: register a pending approval,
    surface it, and await the operator up to ``timeout_s``. Approved→True,
    declined→False, timeout→:class:`MCPApprovalTimeout` (dispatcher → 202). The
    pending entry is always discarded when the wait ends without a decision
    (timeout OR client disconnect) so ``_pending`` never accumulates ghosts."""
    import uuid

    async def _ask(verb: str, params: dict[str, Any], client: MCPClient) -> bool:
        approval_id = uuid.uuid4().hex
        future = registry.create(approval_id, verb, client.name)
        try:
            emit(approval_id, verb, client)
        except Exception:
            log.exception("mcp approval emit failed for %s (%s)", verb, approval_id)
        try:
            # `shield` so a wait_for timeout cannot CANCEL the decision
            # future out from under a same-instant `resolve()` — the settle
            # is atomic on the future itself (single loop thread): whichever
            # of decision/timeout lands first wins, deterministically.
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout_s)
        except asyncio.TimeoutError:
            if future.done() and not future.cancelled():
                # The operator decided in the same tick the hold expired —
                # honor the decision instead of discarding it (the Deferred
                # approve-at-boundary race, trio W1).
                return future.result()
            registry.discard(approval_id)
            raise MCPApprovalTimeout(approval_id)
        except asyncio.CancelledError:
            # MCP client disconnected while the request was held — drop the
            # entry so it doesn't linger as a phantom pending approval.
            registry.discard(approval_id)
            raise

    return _ask


__all__ = [
    "MCPApprovalRegistry",
    "MCPApprovalTimeout",
    "build_verb_ask_fn",
    "ApprovalEmit",
]
