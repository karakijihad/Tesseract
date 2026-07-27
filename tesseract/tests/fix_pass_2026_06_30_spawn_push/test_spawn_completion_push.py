"""Spawn push-on-completion (Stage 1, 2026-06-30).

Background delegations used to surface completion only via the UI-only
``SPAWN_DONE`` chunk — the LLM never saw a finished spawn, so TARS "forgot"
to act on background work. Stage 1 wires a ``completion_notifier`` on
``SpawnRegistry`` that the owning ``ChatSession`` uses to queue a one-shot
``[spawn_completed]`` note, surfaced on the next turn's iteration-0 injection
(same mechanism as conscience notes). These tests prove the brain-layer floor;
the idle-wake (Mirror turn-driver) is Stage 2.

Fakes/light construction only — nothing writes under ``tesseract/logs/``.
"""

from __future__ import annotations

import asyncio

import pytest

from tesseract.brain.chat import ChatSession
from tesseract.brain.spawns import SpawnRegistry
from tesseract.kernel.adapters.base import AdapterOptions
from tesseract.kernel.tools.base import ToolResult


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    # Defensive: keep any incidental runtime writes off the real tree.
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))


async def _ok_result() -> ToolResult:
    return ToolResult(output="hello world\nsecond line")


def _new_session() -> ChatSession:
    return ChatSession(
        adapter=None,  # type: ignore[arg-type]
        system_prompt="",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(),
    )


@pytest.mark.asyncio
async def test_registry_notifier_fires_once_on_completion() -> None:
    reg = SpawnRegistry()
    seen: list[str] = []
    reg.completion_notifier = lambda h: seen.append(h.handle_id)

    handle = reg.register(kind="delegate_claude", coro=_ok_result())
    await handle.task
    await asyncio.sleep(0)  # let the done-callback run

    assert seen == [handle.handle_id]


@pytest.mark.asyncio
async def test_notifier_exception_is_swallowed() -> None:
    reg = SpawnRegistry()

    def _boom(_h):
        raise RuntimeError("notifier blew up")

    reg.completion_notifier = _boom
    handle = reg.register(kind="delegate_codex", coro=_ok_result())
    # Must not raise — _on_done guards the notifier.
    await handle.task
    await asyncio.sleep(0)
    assert handle.task.done()


@pytest.mark.asyncio
async def test_cancelled_spawn_does_not_notify() -> None:
    """An operator-cancelled spawn (/reset's cancel_all or spawn_cancel) is not
    a completion — it must not nudge TARS, and must not re-populate a deque
    that reset() just cleared."""
    reg = SpawnRegistry()
    seen: list[str] = []
    reg.completion_notifier = lambda h: seen.append(h.handle_id)

    async def _slow() -> ToolResult:
        await asyncio.sleep(10)
        return ToolResult(output="never")

    handle = reg.register(kind="delegate_claude", coro=_slow())
    await reg.cancel(handle.handle_id)
    await asyncio.sleep(0)

    assert seen == []


def test_chat_session_wires_notifier() -> None:
    cs = _new_session()
    assert cs.spawns.completion_notifier == cs.ingest_spawn_completion


@pytest.mark.asyncio
async def test_completed_spawn_surfaces_in_next_turn_injection() -> None:
    cs = _new_session()
    cs.history = [{"role": "user", "content": "go"}]

    handle = cs.spawns.register(kind="delegate_claude", coro=_ok_result())
    await handle.task
    await asyncio.sleep(0)  # notifier → ingest_spawn_completion

    assert len(cs._pending_spawn_completions) == 1

    injection = cs._drain_pending_suggestions()
    assert "[spawn_completed]" in injection
    assert handle.handle_id in injection
    assert "spawn_await" in injection  # tells TARS how to get full output
    # one-shot: drained, not re-emitted
    assert len(cs._pending_spawn_completions) == 0
    assert cs._drain_pending_suggestions() == ""


def test_ingest_is_idempotent_per_drain_and_capped() -> None:
    cs = _new_session()

    class _FakeHandle:
        def __init__(self, hid: str) -> None:
            self.handle_id = hid
            self.kind = "delegate_claude"

        def status(self) -> str:
            return "done"

    # Cap is 8 — pushing 10 keeps only the newest 8.
    for i in range(10):
        cs.ingest_spawn_completion(_FakeHandle(f"del-{i}"))
    assert len(cs._pending_spawn_completions) == 8
    injection = cs._drain_pending_suggestions()
    assert "del-9" in injection
    assert "del-0" not in injection  # oldest dropped by the bounded deque


def test_reset_clears_pending_spawn_completions() -> None:
    cs = _new_session()

    class _FakeHandle:
        handle_id = "del-x"
        kind = "delegate_claude"

        def status(self) -> str:
            return "done"

    cs.ingest_spawn_completion(_FakeHandle())
    assert len(cs._pending_spawn_completions) == 1
    cs.reset()
    assert len(cs._pending_spawn_completions) == 0
