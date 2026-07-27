"""Spawn push-on-completion Stage 2B — halt watchdog (2026-06-30).

A background spawn stuck `running` past the configured bound shouldn't sit
forever silently. `SpawnRegistry.sweep_stalled` flags such handles once;
`ChatSession._sweep_stalled_spawns` queues a one-shot `[spawn_stalled]` note at
turn start (riding Stage 1's floor injection). The bound is config-driven
(`runtime.yaml::spawn_stall_seconds`, raise-loudly).

Fakes / light construction only — nothing writes under ``tesseract/logs/``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from tesseract.brain import failures_signal
from tesseract.brain.chat import ChatSession
from tesseract.brain.spawns import SpawnRegistry
from tesseract.kernel.adapters.base import AdapterOptions
from tesseract.kernel.tools.base import ToolResult
from tesseract.config.runtime_limits import load_spawn_stall_seconds


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    # P6 Task 3 §G4 — `_sweep_stalled_spawns` now bumps the process-global
    # digest failures counter; reset it so this suite's stall sweeps don't
    # leak into another test file's autonomy-digest assertions when run in
    # the same pytest session.
    failures_signal.reset_for_tests()
    yield
    failures_signal.reset_for_tests()


async def _slow() -> ToolResult:
    await asyncio.sleep(30)
    return ToolResult(output="never")


async def _ok() -> ToolResult:
    return ToolResult(output="done")


def _new_session(**kw) -> ChatSession:
    return ChatSession(
        adapter=None,  # type: ignore[arg-type]
        system_prompt="",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(),
        **kw,
    )


# --- SpawnRegistry.sweep_stalled -----------------------------------------


@pytest.mark.asyncio
async def test_running_spawn_past_bound_flagged_once() -> None:
    reg = SpawnRegistry()
    h = reg.register(kind="delegate_claude", coro=_slow())
    try:
        assert reg.sweep_stalled(60) == []          # fresh → not stalled
        future = datetime.now(timezone.utc) + timedelta(seconds=120)
        stalled = reg.sweep_stalled(60, now=future)
        assert [s.handle_id for s in stalled] == [h.handle_id]
        assert reg.sweep_stalled(60, now=future) == []   # dedup: once only
    finally:
        await reg.cancel(h.handle_id)


@pytest.mark.asyncio
async def test_done_spawn_not_flagged() -> None:
    reg = SpawnRegistry()
    h = reg.register(kind="delegate_claude", coro=_ok())
    await h.task
    await asyncio.sleep(0)
    future = datetime.now(timezone.utc) + timedelta(seconds=120)
    assert reg.sweep_stalled(1, now=future) == []   # not running → skipped


# --- ChatSession._sweep_stalled_spawns -----------------------------------


@pytest.mark.asyncio
async def test_chat_sweep_noop_when_disabled() -> None:
    cs = _new_session()  # spawn_stall_seconds defaults to None
    assert cs.spawn_stall_seconds is None
    h = cs.spawns.register(kind="delegate_claude", coro=_slow())
    try:
        cs._sweep_stalled_spawns()
        assert len(cs._pending_spawn_completions) == 0
    finally:
        await cs.spawns.cancel(h.handle_id)


@pytest.mark.asyncio
async def test_chat_sweep_enqueues_stall_note() -> None:
    cs = _new_session(spawn_stall_seconds=0.01)
    h = cs.spawns.register(kind="delegate_claude", coro=_slow())
    try:
        await asyncio.sleep(0.02)               # age now exceeds the bound
        cs._sweep_stalled_spawns()
        assert len(cs._pending_spawn_completions) == 1
        # surfaces in the turn injection (same floor as completions)
        injection = cs._drain_pending_suggestions()
        assert "[spawn_stalled]" in injection
        assert h.handle_id in injection
        assert "spawn_cancel" in injection
    finally:
        await cs.spawns.cancel(h.handle_id)


# --- config loader (raise-loudly) ----------------------------------------


def _write_yaml(tmp_path, body: str):
    p = tmp_path / "runtime.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_loader_returns_value(tmp_path) -> None:
    p = _write_yaml(tmp_path, "spawn_stall_seconds: 900\n")
    assert load_spawn_stall_seconds(p) == 900.0


def test_loader_raises_on_missing_key(tmp_path) -> None:
    p = _write_yaml(tmp_path, "other: 1\n")
    with pytest.raises(ValueError, match="spawn_stall_seconds"):
        load_spawn_stall_seconds(p)


def test_loader_raises_on_non_positive(tmp_path) -> None:
    p = _write_yaml(tmp_path, "spawn_stall_seconds: 0\n")
    with pytest.raises(ValueError, match="> 0"):
        load_spawn_stall_seconds(p)
