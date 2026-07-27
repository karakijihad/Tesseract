"""P6 Task 3 §G5 — SpawnRegistry journal writes + ChatSession resume-time
vanished-spawn marking.

`SpawnRegistry.session_id` (set additively by `ChatSession.__post_init__`
from `tool_context.session_id`; `None` disables journaling — REPL/synthetic
sessions unaffected) turns on best-effort start/terminal journaling.
`ChatSession.mark_vanished_spawns` reads that journal at resume time and
enqueues one one-shot `[spawn_lost]` note per orphan (a `start` with no
matching `terminal`) — idempotent by construction, since the sweep itself
marks each orphan terminal.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tesseract.brain import failures_signal, spawn_journal
from tesseract.brain.chat import ChatSession
from tesseract.brain.spawns import SpawnRegistry
from tesseract.kernel.adapters.base import AdapterOptions
from tesseract.kernel.tools.base import ToolContext, ToolResult


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    failures_signal.reset_for_tests()
    yield
    failures_signal.reset_for_tests()


def _new_session(**kw) -> ChatSession:
    return ChatSession(
        adapter=None,  # type: ignore[arg-type]
        system_prompt="",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(),
        **kw,
    )


async def _ok() -> ToolResult:
    return ToolResult(output="done")


# --- ChatSession <-> SpawnRegistry session_id threading -------------------


def test_session_id_threaded_from_tool_context():
    cs = _new_session(tool_context=ToolContext(session_id="sess-live"))
    assert cs.spawns.session_id == "sess-live"


def test_session_id_stays_none_when_tool_context_blank():
    cs = _new_session()  # default ToolContext(), session_id=""
    assert cs.spawns.session_id is None


# --- SpawnRegistry <-> journal wiring --------------------------------------


@pytest.mark.asyncio
async def test_register_writes_start_and_terminal_events_when_session_id_set():
    reg = SpawnRegistry()
    reg.session_id = "sess-1"
    h = reg.register(kind="delegate_claude", coro=_ok())
    await h.task
    await asyncio.sleep(0)  # let the done-callback run

    lines = spawn_journal.spawn_journal_path("sess-1").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert '"start"' in lines[0] and h.handle_id in lines[0]
    assert '"terminal"' in lines[1] and h.handle_id in lines[1]


@pytest.mark.asyncio
async def test_register_no_journal_dir_when_session_id_unset(tmp_path):
    reg = SpawnRegistry()  # session_id stays None
    h = reg.register(kind="delegate_claude", coro=_ok())
    await h.task
    await asyncio.sleep(0)

    assert not (tmp_path / "logs" / "sessions").exists()


@pytest.mark.asyncio
async def test_journal_write_failure_does_not_block_spawn_execution(monkeypatch):
    """Journal IO error must never block or fail spawn execution — same
    discipline as memory writes (CLAUDE.md)."""

    def _boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(spawn_journal, "record_start", _boom)
    monkeypatch.setattr(spawn_journal, "record_terminal", _boom)

    reg = SpawnRegistry()
    reg.session_id = "sess-1"
    h = reg.register(kind="delegate_claude", coro=_ok())  # must not raise
    result = await h.task
    await asyncio.sleep(0)
    assert result.output == "done"


# --- ChatSession.mark_vanished_spawns (resume sweep) -----------------------


def test_orphan_enqueues_spawn_lost_note_exactly_once():
    spawn_journal.record_start("sess-old", "h1", "delegate_claude", "t0")

    cs = _new_session()
    count = cs.mark_vanished_spawns("sess-old")

    assert count == 1
    assert len(cs._pending_spawn_completions) == 1
    note = cs._pending_spawn_completions[0]
    assert "[spawn_lost]" in note
    assert "h1" in note
    assert failures_signal.vanished_count() == 1


def test_orphan_idempotent_across_second_restore():
    spawn_journal.record_start("sess-old", "h1", "delegate_claude", "t0")

    cs1 = _new_session()
    cs1.mark_vanished_spawns("sess-old")

    cs2 = _new_session()  # simulates a second restore/reconnect of the same chat
    count2 = cs2.mark_vanished_spawns("sess-old")

    assert count2 == 0
    assert len(cs2._pending_spawn_completions) == 0
    assert failures_signal.vanished_count() == 1  # not double-counted


def test_terminated_handle_stays_silent():
    spawn_journal.record_start("sess-old", "h1", "delegate_claude", "t0")
    spawn_journal.record_terminal("sess-old", "h1", "done")  # completed normally, pre-restart

    cs = _new_session()
    count = cs.mark_vanished_spawns("sess-old")

    assert count == 0
    assert len(cs._pending_spawn_completions) == 0
    assert failures_signal.vanished_count() == 0


def test_blank_session_id_is_noop():
    cs = _new_session()
    assert cs.mark_vanished_spawns("") == 0
    assert len(cs._pending_spawn_completions) == 0
