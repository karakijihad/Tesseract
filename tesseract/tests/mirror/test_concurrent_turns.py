"""mirror-multi-chat P2 inc.C — per-chat turn lifecycle + concurrent substrate.

Scoped concurrent-streaming substrate: per-chat task/queue dicts with active-slot
back-compat accessors, the ``send_and_await_turn`` conductor primitive, the
per-session stream lock that serializes chat turns, and background-chat TTS
suppression (D8 — voice speaks to the active chat only). No real model or disk —
stub ChatSessions stand in.
"""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tesseract.mirror.server import ws as ws_module
from tesseract.mirror.server import turn_runner as turn_runner_module
from tesseract.mirror.server.session import ServerSession
from tesseract.mirror.server.turn_context import tts_suppressed


def _fake_cs(tag: str = "cs") -> SimpleNamespace:
    return SimpleNamespace(tag=tag, history=[], pending_injected_messages=[])


def _session() -> ServerSession:
    return ServerSession(
        session_id="sess-inc-c",
        ws=SimpleNamespace(closed=False, send_json=AsyncMock()),
        chat_session=_fake_cs("c0"),
        event_log=SimpleNamespace(append=lambda *_: None),
    )


class _FakeTask:
    """Stands in for an asyncio.Task — only ``done()`` is exercised here."""

    def __init__(self, done: bool = False) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


# ── Group A — per-chat state mechanics (pure) ────────────────────────


def test_current_turn_task_property_routes_to_active_slot() -> None:
    s = _session()
    active = s.active_chat_id
    assert s.current_turn_task is None
    t = _FakeTask()
    s.current_turn_task = t
    assert s.current_turn_tasks[active] is t
    assert s.current_turn_task is t
    s.current_turn_task = None
    assert active not in s.current_turn_tasks
    assert s.current_turn_task is None


def test_chat_queues_holds_per_chat_fifo_backlog() -> None:
    """conversation-layer Task 4.2 (Q2) — follow-ups append to the active
    chat's FIFO queue directly (Task 4.5 retired the `pending_user_payload`
    single-slot back-compat view over it)."""
    s = _session()
    active = s.active_chat_id
    assert active not in s.chat_queues
    s.chat_queues.setdefault(active, deque()).append({"text": "hi"})
    assert list(s.chat_queues[active]) == [{"text": "hi"}]
    s.chat_queues.pop(active, None)
    assert active not in s.chat_queues


def test_has_running_turn_spans_all_chats() -> None:
    s = _session()
    assert not s.has_running_turn()
    s.current_turn_tasks["bg"] = _FakeTask(done=False)
    assert s.has_running_turn()
    s.current_turn_tasks["bg"] = _FakeTask(done=True)
    assert not s.has_running_turn()


def test_property_follows_active_after_switch_and_isolates_per_chat() -> None:
    s = _session()
    first = s.active_chat_id
    s.current_turn_tasks[first] = _FakeTask()
    cid2 = "f" * 32
    s.chats[cid2] = _fake_cs("c1")
    s.chat_order.append(cid2)
    s.switch_chat(cid2)
    # The active-slot accessor now follows cid2 (empty), while the first
    # chat's task stays tracked independently in the dict.
    assert s.current_turn_task is None
    s.current_turn_task = _FakeTask()
    assert cid2 in s.current_turn_tasks
    assert first in s.current_turn_tasks
    assert s.has_running_turn()


# ── Group B — send_and_await_turn + the per-session stream lock ───────


def _patch_spawn() -> "patch":
    return patch.object(
        ws_module, "_spawn_tracked", lambda app, coro, name: asyncio.create_task(coro)
    )


@pytest.mark.asyncio
async def test_send_and_await_runs_chat_and_clears_slot() -> None:
    s = _session()
    seen: dict = {}

    async def fake_run(app, session, text, attachments=None, *, chat_id=None, outcome=None):
        seen["chat_id"] = chat_id
        seen["text"] = text
        seen["registered"] = session.current_turn_tasks.get(chat_id) is not None

    with patch.object(turn_runner_module, "_run_turn", new=fake_run), _patch_spawn():
        await ws_module.send_and_await_turn({}, s, "bg-chat", "hello")

    assert seen["chat_id"] == "bg-chat"
    assert seen["text"] == "hello"
    assert seen["registered"] is True  # slot held while the turn runs
    assert "bg-chat" not in s.current_turn_tasks  # freed afterwards


@pytest.mark.asyncio
async def test_active_chat_turns_serialize_on_lock() -> None:
    """inc.C2 — the stream lock is now active-turn-only. Two turns on the ACTIVE
    chat still serialize (D8: the active chat owns the voice; a second send waits
    for the first to finish)."""
    s = _session()
    active = s.active_chat_id
    events: list[tuple[str, str]] = []

    async def fake_run(app, session, text, attachments=None, *, chat_id=None, outcome=None):
        events.append(("enter", chat_id))
        await asyncio.sleep(0.02)
        events.append(("exit", chat_id))

    with patch.object(turn_runner_module, "_run_turn", new=fake_run), _patch_spawn():
        await asyncio.gather(
            ws_module.send_and_await_turn({}, s, active, "a"),
            ws_module.send_and_await_turn({}, s, active, "b"),
        )

    # No interleave: each enter is immediately followed by ITS OWN exit.
    assert events[0] == ("enter", active)
    assert events[1] == ("exit", active)
    assert events[2] == ("enter", active)
    assert events[3] == ("exit", active)


@pytest.mark.asyncio
async def test_background_turns_stream_in_parallel() -> None:
    """inc.C2 — non-active (background/conductor) chats run lock-free so their
    text streams concurrently. Both turns ENTER before either EXITs."""
    s = _session()  # active chat is the seeded c0; chatA/chatB are background
    events: list[tuple[str, str]] = []

    async def fake_run(app, session, text, attachments=None, *, chat_id=None, outcome=None):
        events.append(("enter", chat_id))
        await asyncio.sleep(0.02)
        events.append(("exit", chat_id))

    with patch.object(turn_runner_module, "_run_turn", new=fake_run), _patch_spawn():
        await asyncio.gather(
            ws_module.send_and_await_turn({}, s, "chatA", "a"),
            ws_module.send_and_await_turn({}, s, "chatB", "b"),
        )

    # Lock-free interleave: the first two events are both enters.
    assert events[0][0] == "enter"
    assert events[1][0] == "enter"
    assert {events[0][1], events[1][1]} == {"chatA", "chatB"}


@pytest.mark.asyncio
async def test_provider_semaphore_bounds_same_provider_turns() -> None:
    """inc.C2 — even lock-free background turns must not exceed the per-provider
    concurrency cap. With cap=1 on a single provider, two background turns
    serialize on the semaphore."""
    s = _session()
    app = {
        "chat_turn_semaphores": {},
        "max_concurrent_chat_turns_per_provider": 1,
        "adapter_entry": SimpleNamespace(provider="anthropic"),
    }
    events: list[tuple[str, str]] = []

    async def fake_run(a, session, text, attachments=None, *, chat_id=None, outcome=None):
        events.append(("enter", chat_id))
        await asyncio.sleep(0.02)
        events.append(("exit", chat_id))

    with patch.object(turn_runner_module, "_run_turn", new=fake_run), _patch_spawn():
        await asyncio.gather(
            ws_module.send_and_await_turn(app, s, "chatA", "a"),
            ws_module.send_and_await_turn(app, s, "chatB", "b"),
        )

    # cap=1 serializes: each enter immediately followed by its own exit.
    assert events[0][0] == "enter"
    assert events[1] == ("exit", events[0][1])
    assert events[2][0] == "enter"
    assert events[3] == ("exit", events[2][1])


@pytest.mark.asyncio
async def test_run_turns_concurrently_isolates_failures() -> None:
    """inc.C2 — the conductor fan-out gathers per-chat turns with
    return_exceptions=True: one chat erroring must not abort the others."""
    s = _session()

    async def fake_run(app, session, text, attachments=None, *, chat_id=None, outcome=None):
        if chat_id == "bad":
            raise RuntimeError("boom")

    with patch.object(turn_runner_module, "_run_turn", new=fake_run), _patch_spawn():
        results = await ws_module.run_turns_concurrently(
            {}, s, [("good", "a"), ("bad", "b")]
        )

    assert results[0] is None  # good chat completed
    assert isinstance(results[1], Exception)  # bad chat's error is isolated, not raised
    assert str(results[1]) == "boom"


@pytest.mark.asyncio
async def test_chat_switch_cancels_outgoing_tts() -> None:
    """inc.C2 dynamic voice — switching the active chat cancels the chat we are
    leaving: its in-flight TTS synth is cancelled and its buffer cleared so the
    already-queued audio doesn't trail on after the switch."""
    s = _session()
    target = s.create_chat(_fake_cs("c1"))  # registers chats + chat_meta + order

    async def _never() -> None:
        await asyncio.sleep(10)

    synth = asyncio.create_task(_never())
    s.tts_synth_task = synth
    s.tts_buffer = "half a sentence"

    await ws_module._handle_chat_switch({}, s, {"chat_id": target})

    assert s.active_chat_id == target
    assert s.tts_synth_task is None
    assert s.tts_buffer == ""
    with pytest.raises(asyncio.CancelledError):
        await synth


# ── Group C — background-chat TTS suppression (D8), real _run_turn ────


@pytest.mark.asyncio
async def test_background_turn_suppresses_tts_active_keeps_voice() -> None:
    captured: dict[str, bool] = {}

    class _CapCS:
        history: list = []

        def __init__(self, label: str) -> None:
            self.label = label
            self.pending_injected_messages: list = []

        async def send(self, *a, **k):
            # Capture the live TTS gate for this chat (dynamic — cid vs active).
            captured[self.label] = tts_suppressed(s)
            return
            yield  # pragma: no cover — make this an async generator

    s = _session()
    active = s.active_chat_id
    cap_active = _CapCS("active")
    s.chats[active] = cap_active
    s.chat_session = cap_active

    bg = "a" * 32
    cap_bg = _CapCS("bg")
    s.chats[bg] = cap_bg
    s.chat_order.append(bg)

    app: dict = {"adapter_options": None, "mood": None, "memory_bundle": None}
    with (
        patch.object(turn_runner_module, "_maybe_auto_compact", new=AsyncMock(return_value=None)),
        patch.object(turn_runner_module, "emit_stats", new=AsyncMock(return_value=None)),
    ):
        await ws_module._run_turn(app, s, "hi", chat_id=active)
        await ws_module._run_turn(app, s, "hi", chat_id=bg)

    assert captured["active"] is False  # active chat speaks
    assert captured["bg"] is True       # background chat stays silent (D8)
    # current_chat_id resets after every turn → no turn active → audible.
    assert tts_suppressed(s) is False


@pytest.mark.asyncio
async def test_compact_and_stats_target_the_chat_that_ran() -> None:
    """A background turn must compact / report stats for ITS chat, not the
    active one (the active `session.chat_session` would be the wrong target)."""

    class _EmptyCS:
        history: list = []
        pending_injected_messages: list = []

        async def send(self, *a, **k):
            return
            yield  # pragma: no cover — async generator

    s = _session()
    bg = "b" * 32
    bg_cs = _EmptyCS()
    s.chats[bg] = bg_cs
    s.chat_order.append(bg)

    captured: dict = {}

    async def fake_compact(app, session, cs=None):
        captured["compact_cs"] = cs

    async def fake_stats(app, session, cs=None):
        captured["stats_cs"] = cs

    app: dict = {"adapter_options": None, "mood": None, "memory_bundle": None}
    with (
        patch.object(turn_runner_module, "_maybe_auto_compact", new=fake_compact),
        patch.object(turn_runner_module, "emit_stats", new=fake_stats),
    ):
        await ws_module._run_turn(app, s, "hi", chat_id=bg)

    assert captured["compact_cs"] is bg_cs  # NOT s.chat_session (the active chat)
    assert captured["stats_cs"] is bg_cs


@pytest.mark.asyncio
async def test_background_turn_end_rescues_stranded_steer_inject() -> None:
    """Review fix-pass Finding 1 — a Q3-steer inject stranded on a
    BACKGROUND chat (landed via `handle_steer`, but the chat's OWN turn
    ended before a tool boundary ever drained `pending_injected_messages`)
    must be rescued at THAT turn's own end, not silently dropped. Pre-fix,
    `_run_turn`'s tail only ever called `drain_next`, and only when
    `cid == session.active_chat_id` — a background chat's stranded inject
    was never reached even though the operator already saw a `steered`
    envelope confirming it landed."""

    class _StrandedCS:
        history: list = []

        def __init__(self) -> None:
            self.pending_injected_messages = [
                {"text": "stranded background steer", "queued_at": "stub"}
            ]

        async def send(self, *a, **k):
            return
            yield  # pragma: no cover — async generator

    s = _session()
    bg = "b" * 32
    bg_cs = _StrandedCS()
    s.chats[bg] = bg_cs
    s.chat_order.append(bg)

    rescued: dict = {}

    async def fake_rescue(app, session, chat_id):
        rescued["chat_id"] = chat_id
        rescued["pending"] = list(session.chats[chat_id].pending_injected_messages)

    from tesseract.mirror.server import turn_intake as turn_intake_module

    app: dict = {"adapter_options": None, "mood": None, "memory_bundle": None}
    with (
        patch.object(turn_runner_module, "_maybe_auto_compact", new=AsyncMock(return_value=None)),
        patch.object(turn_runner_module, "emit_stats", new=AsyncMock(return_value=None)),
        patch.object(turn_intake_module, "drain_stranded_background", new=fake_rescue),
        patch.object(turn_intake_module, "drain_next", new=AsyncMock()) as drain_next_mock,
    ):
        await ws_module._run_turn(app, s, "hi", chat_id=bg)

    assert rescued["chat_id"] == bg
    assert rescued["pending"] == [{"text": "stranded background steer", "queued_at": "stub"}]
    drain_next_mock.assert_not_awaited()  # background chat, not the focused-chat path
