"""conversation-layer Task 5.1 (Q3) — backend `steer` WS command.

Redirects a running turn WITHOUT cancelling it: `handle_steer` folds the
operator's text into the CURRENT turn via `ChatSession.enqueue_user_inject`
(consumed at the next tool boundary) rather than queuing a new turn. No
active turn for the target chat degrades to a normal `_start_turn` send.

SAFETY (review blocker): a steer must NEVER resolve a pending ASK. ASK
futures live on `session.pending_asks` (keyed by call_id, settled only by
`ws._resolve_ask`); `handle_steer` only ever touches the per-chat inject
queue and never reads or writes `pending_asks`. Verified explicitly below.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from pydantic import BaseModel

from tesseract.kernel.tools.base import ToolContext
from tesseract.mirror.server import ask_gate, turn_intake, ws as ws_mod
from tesseract.mirror.server.session import ServerSession


def _build_app() -> web.Application:
    app = web.Application()
    app["mood"] = None
    app["adapter_options"] = None
    app["config"] = SimpleNamespace(
        uploads=SimpleNamespace(max_files_per_message=10, max_total_mb=25),
    )
    return app


def _make_session(session_id: str = "sess-steer") -> ServerSession:
    ws = MagicMock()
    ws.send_json = AsyncMock()
    ws.closed = False
    pending_injected: list[dict] = []

    def _enqueue(text: str) -> None:
        text = (text or "").strip()
        if text:
            pending_injected.append({"text": text, "queued_at": "stub"})

    chat_session = SimpleNamespace(
        tool_context=SimpleNamespace(cancel_event=asyncio.Event()),
        pending_injected_messages=pending_injected,
        enqueue_user_inject=_enqueue,
    )
    return ServerSession(
        session_id=session_id,
        ws=ws,
        chat_session=chat_session,  # type: ignore[arg-type]
        event_log=MagicMock(append=MagicMock()),
    )


async def test_steer_mid_turn_injects_into_pending_injected_messages(monkeypatch):
    """Steer while a turn is active for that chat: text lands in
    `pending_injected_messages` (picked up at the next tool boundary), NOT
    the FIFO turn queue — and a `steered` envelope confirms it landed."""
    app = _build_app()
    session = _make_session()
    cid = session.active_chat_id

    sent: list[dict] = []

    async def fake_send(sess, env):
        if env is not None:
            sent.append(env)

    monkeypatch.setattr(turn_intake, "send_envelope", fake_send)

    async def _busy():
        await asyncio.sleep(60)

    session.current_turn_task = asyncio.create_task(_busy())
    try:
        await turn_intake.handle_steer(app, session, {"chat_id": cid, "text": "actually, do X instead"})

        assert session.chat_session.pending_injected_messages == [
            {"text": "actually, do X instead", "queued_at": "stub"}
        ]
        assert session.chat_queues.get(cid) in (None, [])

        steered_envs = [e for e in sent if e["type"] == "steered"]
        assert len(steered_envs) == 1
        assert steered_envs[0]["data"]["text"] == "actually, do X instead"
        assert steered_envs[0]["chat_id"] == cid
    finally:
        session.current_turn_task.cancel()
        try:
            await session.current_turn_task
        except asyncio.CancelledError:
            pass


async def test_steer_no_active_turn_degrades_to_start_turn(monkeypatch):
    """No turn active for the target chat — nothing to redirect — so
    `handle_steer` degrades to the normal `_start_turn` send path instead
    of silently dropping the text.

    Review fix-pass (Task 5.2): the frontend already rendered this steer's
    bubble optimistically as `steered: true` before this handler ever ran
    (`sendSteer` in conversation.ts). Since there is nothing to redirect,
    `handle_steer` must emit a `steered` envelope with `applied: false` so
    the client can clear that flag instead of leaving a permanent, wrong
    "redirected" pill on what is actually a normal turn."""
    app = _build_app()
    session = _make_session()
    cid = session.active_chat_id
    session.current_turn_task = None  # explicitly no active turn

    started = AsyncMock()
    monkeypatch.setattr(turn_intake, "_start_turn", started)

    sent: list[dict] = []

    async def fake_send(sess, env):
        if env is not None:
            sent.append(env)

    monkeypatch.setattr(turn_intake, "send_envelope", fake_send)

    data = {"chat_id": cid, "text": "hello"}
    await turn_intake.handle_steer(app, session, data)

    started.assert_awaited_once_with(app, session, data)
    # No inject happened on the degrade path.
    assert session.chat_session.pending_injected_messages == []

    steered_envs = [e for e in sent if e["type"] == "steered"]
    assert len(steered_envs) == 1
    assert steered_envs[0]["data"]["applied"] is False
    assert steered_envs[0]["data"]["text"] == "hello"
    assert steered_envs[0]["chat_id"] == cid


class _AskInput(BaseModel):
    path: str = "x.txt"


async def test_steer_never_resolves_a_pending_ask(monkeypatch):
    """SAFETY (review blocker, strengthened): drives a REAL ask via
    `ask_gate._make_ask_fn` (same harness as `tests/trio/test_ask_park.py`)
    instead of stapling a hand-made future into `pending_asks` directly —
    proves `handle_steer` mid-pending-ask doesn't touch the real gate
    machinery, AND that the ask flow completes normally afterward through
    the real `ws._resolve_ask`, not just that a decoy future stays pending.
    `ask_fn`'s durable ledger append (`approval_log.record_ask`) resolves
    `TESSERACT_HOME` at call time — the repo's global autouse per-test tmp
    dir (`tests/conftest.py::_isolate_tesseract_home`) keeps it off
    `tesseract/logs/` without this test needing its own fixture."""
    monkeypatch.setattr(ask_gate, "ASK_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(ask_gate, "ASK_GRACE_SECONDS", 0.5)

    app = _build_app()
    session = _make_session()
    cid = session.active_chat_id

    sent: list[dict] = []

    async def fake_send(sess, env):
        if env is not None:
            sent.append(env)

    monkeypatch.setattr(turn_intake, "send_envelope", fake_send)

    # A tool mid-turn is asking the operator something — drive the ACTUAL
    # ask_fn the ask-gate builds (`_make_ask_fn`), not a decoy future.
    ask_fn = ask_gate._make_ask_fn(
        session.ws, session.session_id, session.pending_asks, session.event_log,
    )
    tool = SimpleNamespace(name="file_write")
    ctx = ToolContext(session_id=session.session_id, current_call_id="call-real-ask-1")
    ask_task = asyncio.create_task(ask_fn(tool, _AskInput(), ctx))
    for _ in range(400):
        if "call-real-ask-1" in session.pending_asks:
            break
        await asyncio.sleep(0.005)
    else:
        pytest.fail("ask never registered in session.pending_asks")
    ask_future = session.pending_asks["call-real-ask-1"]

    async def _busy():
        await asyncio.sleep(60)

    session.current_turn_task = asyncio.create_task(_busy())
    try:
        await turn_intake.handle_steer(app, session, {"chat_id": cid, "text": "redirect while asking"})

        # The real ASK future is untouched: still pending, no decision injected.
        assert not ask_future.done()
        assert session.pending_asks.get("call-real-ask-1") is ask_future

        # The steer itself still worked (inject path taken, not dropped).
        assert session.chat_session.pending_injected_messages == [
            {"text": "redirect while asking", "queued_at": "stub"}
        ]

        # The ask flow still completes normally afterward via the real
        # `_resolve_ask` (the actual WS `tool_response` handler path).
        ws_mod._resolve_ask(session, {"call_id": "call-real-ask-1", "approved": True})
        approved = await ask_task
        assert approved is True
        assert "call-real-ask-1" not in session.pending_asks
    finally:
        session.current_turn_task.cancel()
        try:
            await session.current_turn_task
        except asyncio.CancelledError:
            pass
        if not ask_future.done():
            ask_future.cancel()
        if not ask_task.done():
            ask_task.cancel()


async def test_steer_empty_text_is_a_noop(monkeypatch):
    """Blank/whitespace-only steer text is dropped before touching either
    the inject queue or the degrade path — mirrors `_start_turn`'s own
    empty-text guard."""
    app = _build_app()
    session = _make_session()
    cid = session.active_chat_id

    started = AsyncMock()
    monkeypatch.setattr(turn_intake, "_start_turn", started)

    session.current_turn_task = None
    await turn_intake.handle_steer(app, session, {"chat_id": cid, "text": "   "})

    started.assert_not_awaited()
    assert session.chat_session.pending_injected_messages == []


async def test_steer_background_chat_no_active_turn_drops_not_misroutes(monkeypatch):
    """Review finding: `_start_turn` always sends on `session.active_chat_id`
    — it has no way to target an arbitrary chat_id. If a steer names a
    BACKGROUND chat (not the focused one) that has no active turn, degrading
    to `_start_turn` would silently start a turn in the wrong (focused)
    chat. `handle_steer` must drop it instead of misrouting — and (Finding 2,
    fix-pass) the drop must be client-visible, not just a log line: house
    convention is that drops are never silent (cf. `make_queue_overflow`)."""
    app = _build_app()
    session = _make_session()
    active_cid = session.active_chat_id
    background_cid = "some-other-open-chat"
    session.chats[background_cid] = SimpleNamespace(
        pending_injected_messages=[],
        enqueue_user_inject=lambda text: None,
    )
    # No entry in current_turn_tasks for background_cid == no active turn.

    started = AsyncMock()
    monkeypatch.setattr(turn_intake, "_start_turn", started)

    sent: list[dict] = []

    async def fake_send(sess, env):
        if env is not None:
            sent.append(env)

    monkeypatch.setattr(turn_intake, "send_envelope", fake_send)

    await turn_intake.handle_steer(
        app, session, {"chat_id": background_cid, "text": "target the background chat"}
    )

    started.assert_not_awaited()
    assert session.active_chat_id == active_cid  # unchanged
    assert session.chats[background_cid].pending_injected_messages == []

    rejected_envs = [e for e in sent if e["type"] == "steer_rejected"]
    assert len(rejected_envs) == 1
    assert rejected_envs[0]["data"]["text"] == "target the background chat"
    assert rejected_envs[0]["chat_id"] == background_cid
    assert background_cid in rejected_envs[0]["data"]["reason"]


async def test_drain_stranded_background_rescues_inject_for_background_chat(monkeypatch):
    """Review fix-pass Finding 1: a Q3-steer inject can strand on a
    BACKGROUND (non-focused) chat too — landed via `handle_steer`, then the
    chat's OWN turn ended before a tool boundary ever drained it. Pre-fix,
    `turn_runner._run_turn`'s tail only ever called `drain_next` when
    `cid == session.active_chat_id`, so this case was silently lost even
    though the operator already saw a `steered` envelope confirming it
    landed. `drain_stranded_background` is the rescue for that chat_id;
    it must target the STRANDED chat directly via `_run_chat_turn(chat_id=
    ...)`, never `_start_turn` (which only ever sends to the focused chat
    and would misroute the rescued text there instead)."""
    from tesseract.mirror.server import turn_runner

    app = _build_app()
    session = _make_session()
    background_cid = "some-other-open-chat"
    session.chats[background_cid] = SimpleNamespace(
        pending_injected_messages=[
            {"text": "stranded background steer", "queued_at": "stub"}
        ],
        enqueue_user_inject=lambda text: None,
    )

    run_chat_turn = AsyncMock()
    monkeypatch.setattr(turn_runner, "_run_chat_turn", run_chat_turn)
    started = AsyncMock()
    monkeypatch.setattr(turn_intake, "_start_turn", started)

    await turn_intake.drain_stranded_background(app, session, background_cid)
    # Let the fire-and-forget task actually run.
    for _ in range(20):
        await asyncio.sleep(0)

    started.assert_not_awaited()  # never the focused-chat degrade path
    run_chat_turn.assert_awaited_once()
    call_args = run_chat_turn.await_args
    assert call_args.args[0] is app
    assert call_args.args[1] is session
    assert call_args.args[2] == "stranded background steer"
    assert call_args.kwargs["chat_id"] == background_cid
    # Rescued turn is registered as this chat's in-flight task, mirroring
    # `spawn_wake.schedule_wake`'s pattern for driving a background turn.
    assert background_cid in session.current_turn_tasks
    # The stranded queue is cleared so it can't re-fire.
    assert session.chats[background_cid].pending_injected_messages == []


async def test_drain_next_stranded_inject_fallback_survives_turn_end_race(monkeypatch):
    """Race note in `handle_steer`: if a steer's inject lands right as the
    turn's tool loop is exiting (no further boundary will poll it), the
    turn's own `finally`-tail calls `drain_next`, whose stranded-inject
    fallback re-enters `_start_turn` for any leftover
    `pending_injected_messages` — the steer is never silently lost even
    if it never gets folded into a live turn iteration."""
    app = _build_app()
    session = _make_session()
    cid = session.active_chat_id

    # Simulate: the steer's enqueue_user_inject already ran (as it would,
    # synchronously, inside handle_steer) but the turn ended before ever
    # draining it at a tool boundary. No FIFO-queued normal turn either.
    session.chat_session.enqueue_user_inject("stranded steer text")
    assert cid not in session.chat_queues

    started = AsyncMock()
    monkeypatch.setattr(turn_intake, "_start_turn", started)

    await turn_intake.drain_next(app, session, cid)

    started.assert_awaited_once()
    call_args = started.await_args
    assert call_args.args[0] is app
    assert call_args.args[1] is session
    assert call_args.args[2]["text"] == "stranded steer text"
    # The stranded queue is cleared by the fallback so it can't re-fire.
    assert session.chat_session.pending_injected_messages == []
