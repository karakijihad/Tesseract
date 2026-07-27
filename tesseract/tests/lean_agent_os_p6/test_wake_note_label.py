"""Task 6.3 — the wake-turn nudge names the workstream that finished.

Before this fix ``_wake_turn`` always sent the same static ``_WAKE_NUDGE``
text regardless of which spawn triggered it — with two or three background
workstreams live, TARS's proactive reaction gave no clue which one it was
reacting to. ``on_spawn_complete`` now stashes the triggering handle's label
(goal snippet, falling back to kind — same precedence Task 6.1 gave the
Activity registry, ``brain/spawns.py::_spawn_record``) and ``wake_nudge_text``
renders it into the turn body.

Fakes only — no live brain/turn-driver; nothing writes under
``tesseract/logs/``.
"""

from __future__ import annotations

import pytest

from tesseract.mirror.server import spawn_wake


class _Session:
    def __init__(self) -> None:
        self.session_id = "sess-wake-label-test"
        self.current_turn_tasks: dict = {}
        self.spawn_wake_pending: set[str] = set()
        self.chats: dict = {}


class _CS:
    def has_pending_spawn_completions(self) -> bool:
        return False


class _HandleWithGoal:
    handle_id = "del-claude-1"
    kind = "delegate_claude"

    def __init__(self, goal: str) -> None:
        self.goal = goal

    def status(self) -> str:
        return "done"


class _HandleNoGoal:
    handle_id = "del-codex-1"
    kind = "delegate_codex"
    goal = None

    def status(self) -> str:
        return "done"


async def _capture_body(monkeypatch) -> dict[str, str]:
    captured: dict[str, str] = {}

    async def _fake_run_chat_turn(app, sess, text, *, chat_id, **kwargs):
        captured["text"] = text

    monkeypatch.setattr(
        "tesseract.mirror.server.turn_runner._run_chat_turn", _fake_run_chat_turn,
    )
    return captured


@pytest.mark.asyncio
async def test_wake_note_names_the_goal_derived_label(monkeypatch) -> None:
    session = _Session()
    session.chats["A"] = _CS()
    captured = await _capture_body(monkeypatch)
    # schedule_wake would spin a real asyncio.Task via _spawn_tracked (needs
    # a live `app["scheduler"]`) — irrelevant to label stashing, so bypass it
    # and drive _wake_turn directly, same as the on_spawn_complete unit tests.
    monkeypatch.setattr(spawn_wake, "schedule_wake", lambda *a, **kw: None)

    handle = _HandleWithGoal(goal="research the crawlspace flooding fix")
    spawn_wake.on_spawn_complete(
        None, session, cs=object(), chat_id="A", handle=handle, floor=lambda h: None,
    )
    await spawn_wake._wake_turn(None, session, "A")

    assert "research the crawlspace flooding fix" in captured["text"]


@pytest.mark.asyncio
async def test_wake_note_falls_back_to_kind_when_no_goal(monkeypatch) -> None:
    session = _Session()
    session.chats["A"] = _CS()
    captured = await _capture_body(monkeypatch)
    monkeypatch.setattr(spawn_wake, "schedule_wake", lambda *a, **kw: None)

    spawn_wake.on_spawn_complete(
        None, session, cs=object(), chat_id="A", handle=_HandleNoGoal(),
        floor=lambda h: None,
    )
    await spawn_wake._wake_turn(None, session, "A")

    assert "delegate_codex" in captured["text"]


@pytest.mark.asyncio
async def test_wake_note_falls_back_to_generic_nudge_when_no_label_stashed(
    monkeypatch,
) -> None:
    """Regression pin: a bare ``_wake_turn`` call with nothing stashed (e.g.
    the pre-existing breaker tests) must still get the original wording —
    no ``on_spawn_complete`` call happened so ``wake_nudge_text`` has nothing
    to pop."""
    session = _Session()
    session.chats["A"] = _CS()
    captured = await _capture_body(monkeypatch)

    await spawn_wake._wake_turn(None, session, "A")

    assert captured["text"] == spawn_wake._WAKE_NUDGE


@pytest.mark.asyncio
async def test_straggler_mid_wake_reschedule_keeps_its_label(monkeypatch) -> None:
    """Deferred 2026-07-12 — a spawn completing while a wake turn is in
    flight re-schedules from ``_wake_turn`` (which has no handle), so the
    label must already be stashed by ``on_spawn_complete``'s busy path, not
    only its idle path. The re-scheduled wake's nudge names the straggler."""
    session = _Session()

    class _CSWithStraggler:
        def has_pending_spawn_completions(self) -> bool:
            return True

    session.chats["A"] = _CSWithStraggler()
    monkeypatch.setattr(spawn_wake, "schedule_wake", lambda *a, **kw: None)

    class _BusyTask:
        def done(self) -> bool:
            return False

    async def _fake_run_chat_turn(app, sess, text, *, chat_id, **kwargs):
        # Mid-wake: the chat is busy (this very turn); a straggler completes.
        sess.current_turn_tasks[chat_id] = _BusyTask()
        spawn_wake.on_spawn_complete(
            None, sess, cs=object(), chat_id=chat_id,
            handle=_HandleWithGoal(goal="straggler workstream"),
            floor=lambda h: None,
        )
        sess.current_turn_tasks.pop(chat_id)  # turn ends → idle again

    monkeypatch.setattr(
        "tesseract.mirror.server.turn_runner._run_chat_turn", _fake_run_chat_turn,
    )

    await spawn_wake._wake_turn(None, session, "A")

    # _wake_turn re-scheduled (pending + idle); the re-scheduled wake's
    # nudge must carry the straggler's goal, not the generic fallback.
    assert "A" in session.spawn_wake_pending
    assert "straggler workstream" in spawn_wake.wake_nudge_text(session, "A")


def test_labelless_completion_clears_a_stale_label(monkeypatch) -> None:
    """A completion whose handle yields no label (no goal, no kind) must not
    let a previous workstream's stale label leak into its wake nudge."""
    session = _Session()
    session.chats["A"] = _CS()
    monkeypatch.setattr(spawn_wake, "schedule_wake", lambda *a, **kw: None)
    spawn_wake._stash_wake_label(session, "A", _HandleWithGoal(goal="old goal"))

    class _HandleBlank:
        handle_id = "blank-1"
        kind = None
        goal = None

        def status(self) -> str:
            return "done"

    spawn_wake.on_spawn_complete(
        None, session, cs=object(), chat_id="A", handle=_HandleBlank(),
        floor=lambda h: None,
    )

    assert spawn_wake.wake_nudge_text(session, "A") == spawn_wake._WAKE_NUDGE


def test_label_is_popped_not_reused_by_a_later_wake(monkeypatch) -> None:
    """The stashed label is one-shot per chat — a second, unrelated wake for
    the same chat_id (no fresh on_spawn_complete call) must not resurface a
    stale label from a previous workstream."""
    session = _Session()
    handle = _HandleWithGoal(goal="first workstream")
    spawn_wake._stash_wake_label(session, "A", handle)

    first = spawn_wake.wake_nudge_text(session, "A")
    second = spawn_wake.wake_nudge_text(session, "A")

    assert "first workstream" in first
    assert second == spawn_wake._WAKE_NUDGE
