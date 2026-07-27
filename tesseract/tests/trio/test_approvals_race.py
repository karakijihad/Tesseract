"""W1 — approve-at-boundary race (Deferred real-fix): the decision future
settles atomically; a decision landing in the same tick the hold expires is
honored, and a decision after expiry deterministically 404s."""

from __future__ import annotations

import asyncio

import pytest

from tesseract.config.mcp import MCPClient
from tesseract.mirror.server.mcp.approvals import (
    MCPApprovalRegistry,
    MCPApprovalTimeout,
    build_verb_ask_fn,
)


def _client() -> MCPClient:
    return MCPClient(name="lane-codex", token_env="X", trust_tier="trusted")


def _ask_fn(registry, timeout_s, emitted):
    return build_verb_ask_fn(
        registry, lambda aid, verb, client: emitted.append(aid), timeout_s
    )


def test_approve_before_timeout(isolated_home):
    async def _run():
        registry = MCPApprovalRegistry()
        emitted: list[str] = []
        ask = _ask_fn(registry, timeout_s=5.0, emitted=emitted)
        task = asyncio.create_task(ask("lane.send", {}, _client()))
        await asyncio.sleep(0)  # let the ask register + emit
        assert registry.resolve(emitted[0], approved=True) is True
        assert await task is True
        assert registry.pending() == []

    asyncio.run(_run())


def test_decline_before_timeout(isolated_home):
    async def _run():
        registry = MCPApprovalRegistry()
        emitted: list[str] = []
        ask = _ask_fn(registry, timeout_s=5.0, emitted=emitted)
        task = asyncio.create_task(ask("lane.send", {}, _client()))
        await asyncio.sleep(0)
        assert registry.resolve(emitted[0], approved=False) is True
        assert await task is False

    asyncio.run(_run())


def test_timeout_then_late_decision_404s(isolated_home):
    async def _run():
        registry = MCPApprovalRegistry()
        emitted: list[str] = []
        ask = _ask_fn(registry, timeout_s=0.01, emitted=emitted)
        with pytest.raises(MCPApprovalTimeout) as exc_info:
            await ask("lane.send", {}, _client())
        # After expiry: no ghost pending entry, and a late POST /decision
        # resolves False → the route surfaces 404.
        assert registry.pending() == []
        assert registry.resolve(exc_info.value.approval_id, approved=True) is False

    asyncio.run(_run())


def test_decision_landing_at_the_boundary_is_honored(isolated_home, monkeypatch):
    """The race: wait_for times out in the same tick the operator decides.
    Simulated deterministically — the future already carries the decision
    when the TimeoutError surfaces; the ask must return it, not discard."""

    async def _run():
        registry = MCPApprovalRegistry()
        emitted: list[str] = []
        ask = _ask_fn(registry, timeout_s=99.0, emitted=emitted)

        real_wait_for = asyncio.wait_for

        async def _race_wait_for(awaitable, timeout):
            # Consume the shield, settle the decision, then behave as if
            # the hold expired in the same instant.
            awaitable.cancel()  # release the shield wrapper
            assert registry.resolve(emitted[0], approved=True) is True
            raise asyncio.TimeoutError

        monkeypatch.setattr(asyncio, "wait_for", _race_wait_for)
        try:
            result = await ask("lane.send", {}, _client())
        finally:
            monkeypatch.setattr(asyncio, "wait_for", real_wait_for)
        assert result is True

    asyncio.run(_run())


def test_client_disconnect_discards_pending(isolated_home):
    async def _run():
        registry = MCPApprovalRegistry()
        emitted: list[str] = []
        ask = _ask_fn(registry, timeout_s=5.0, emitted=emitted)
        task = asyncio.create_task(ask("lane.send", {}, _client()))
        await asyncio.sleep(0)
        assert registry.pending() != []
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert registry.pending() == []

    asyncio.run(_run())
