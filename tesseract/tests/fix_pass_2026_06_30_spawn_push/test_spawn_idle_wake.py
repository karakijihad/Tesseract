"""Spawn push-on-completion Stage 2 — idle-wake autonomous turn (2026-06-30).

Stage 1 surfaces a finished spawn at the *next* turn. Stage 2 (this module)
adds the proactive half: a spawn finishing while the owning chat is idle starts
a wake turn so TARS acts on its own. These tests cover the decision/dedup logic
(`on_spawn_complete`), multi-chat targeting, the re-check-on-end straggler path,
and the notifier override (`wire_chat`) — all without a live brain or
turn-driver (ws `_run_chat_turn` / `_spawn_tracked` are monkeypatched).

Fakes only — nothing writes under ``tesseract/logs/``.
"""

from __future__ import annotations

import pytest

from tesseract.mirror.server import spawn_wake


class _Task:
    def __init__(self, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


class _Session:
    def __init__(self) -> None:
        self.session_id = "sess-test"
        self.current_turn_tasks: dict = {}
        self.spawn_wake_pending: set[str] = set()
        self.chats: dict = {}


class _Handle:
    handle_id = "del-claude-1"
    kind = "delegate_claude"

    def status(self) -> str:
        return "done"


@pytest.fixture
def scheduled(monkeypatch):
    """Record schedule_wake(chat_id) calls instead of spawning a real turn."""
    calls: list[str] = []
    monkeypatch.setattr(
        spawn_wake, "schedule_wake",
        lambda app, session, chat_id: calls.append(chat_id),
    )
    return calls


# --- decision unit -------------------------------------------------------


def test_idle_completion_ingests_and_schedules(scheduled) -> None:
    session = _Session()
    ingested: list = []
    spawn_wake.on_spawn_complete(
        None, session, cs=object(), chat_id="A", handle=_Handle(),
        floor=ingested.append,
    )
    assert len(ingested) == 1            # floor always runs
    assert scheduled == ["A"]            # idle → wake scheduled
    assert "A" in session.spawn_wake_pending


def test_busy_completion_ingests_but_does_not_schedule(scheduled) -> None:
    session = _Session()
    session.current_turn_tasks["A"] = _Task(done=False)  # turn in flight
    ingested: list = []
    spawn_wake.on_spawn_complete(
        None, session, cs=object(), chat_id="A", handle=_Handle(),
        floor=ingested.append,
    )
    assert len(ingested) == 1            # running turn drains it at iter-0
    assert scheduled == []
    assert "A" not in session.spawn_wake_pending


def test_finished_turn_counts_as_idle(scheduled) -> None:
    session = _Session()
    session.current_turn_tasks["A"] = _Task(done=True)  # prior turn finished
    spawn_wake.on_spawn_complete(
        None, session, cs=object(), chat_id="A", handle=_Handle(),
        floor=lambda h: None,
    )
    assert scheduled == ["A"]


def test_burst_dedups_to_one_wake(scheduled) -> None:
    session = _Session()
    floor_calls: list = []
    for _ in range(3):  # three completions land before any wake starts
        spawn_wake.on_spawn_complete(
            None, session, cs=object(), chat_id="A", handle=_Handle(),
            floor=floor_calls.append,
        )
    assert len(floor_calls) == 3          # every note still queued
    assert scheduled == ["A"]             # but only ONE wake scheduled


# --- multi-chat ----------------------------------------------------------


def test_completion_wakes_owning_chat_not_active(scheduled) -> None:
    session = _Session()
    session.current_turn_tasks["A"] = _Task(done=False)  # chat A busy
    # spawn owned by chat B (idle) finishes
    spawn_wake.on_spawn_complete(
        None, session, cs=object(), chat_id="B", handle=_Handle(),
        floor=lambda h: None,
    )
    assert scheduled == ["B"]
    assert "B" in session.spawn_wake_pending
    assert "A" not in session.spawn_wake_pending


# --- re-check on wake end ------------------------------------------------


class _CS:
    def __init__(self, pending_after: bool) -> None:
        self._pending = pending_after

    def has_pending_spawn_completions(self) -> bool:
        return self._pending


@pytest.mark.asyncio
async def test_recheck_schedules_again_when_note_lands_mid_wake(
    monkeypatch, scheduled
) -> None:
    session = _Session()
    session.chats["A"] = _CS(pending_after=True)   # a completion landed mid-wake
    session.spawn_wake_pending.add("A")

    async def _fake_run_chat_turn(app, sess, text, *, chat_id, **kwargs):
        # turn ran and freed its slot (mirrors _run_turn's end-of-turn pop)
        sess.current_turn_tasks.pop(chat_id, None)

    monkeypatch.setattr(
        "tesseract.mirror.server.turn_runner._run_chat_turn", _fake_run_chat_turn
    )
    await spawn_wake._wake_turn(None, session, "A")
    assert scheduled == ["A"]              # straggler note → one more wake


@pytest.mark.asyncio
async def test_recheck_no_reschedule_when_drained(monkeypatch, scheduled) -> None:
    session = _Session()
    session.chats["A"] = _CS(pending_after=False)  # nothing left after the turn
    session.spawn_wake_pending.add("A")

    async def _fake_run_chat_turn(app, sess, text, *, chat_id, **kwargs):
        sess.current_turn_tasks.pop(chat_id, None)

    monkeypatch.setattr(
        "tesseract.mirror.server.turn_runner._run_chat_turn", _fake_run_chat_turn
    )
    await spawn_wake._wake_turn(None, session, "A")
    assert scheduled == []
    assert "A" not in session.spawn_wake_pending  # cleared at wake start


# --- notifier override ---------------------------------------------------


def test_wire_chat_keeps_floor_and_adds_wake(scheduled) -> None:
    from tesseract.brain.chat import ChatSession
    from tesseract.kernel.adapters.base import AdapterOptions

    cs = ChatSession(
        adapter=None,  # type: ignore[arg-type]
        system_prompt="",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(),
    )
    session = _Session()
    session.chats["A"] = cs

    spawn_wake.wire_chat(None, session, "A", cs)
    assert cs.spawns.completion_notifier is not cs.ingest_spawn_completion

    cs.spawns.completion_notifier(_Handle())   # simulate done-callback
    assert len(cs._pending_spawn_completions) == 1  # floor still ran
    assert scheduled == ["A"]                       # wake scheduled (idle)
