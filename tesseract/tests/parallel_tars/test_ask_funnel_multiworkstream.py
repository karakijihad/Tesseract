"""Task 6.3 — SAFETY RE-VERIFICATION (review blocker): the ASK funnel under
concurrent multi-workstream load.

Contract characterization (read before extending this file): the codebase
does NOT enforce literal "one ask processed/displayed at a time" anywhere.

* Backend (``ask_gate._make_ask_fn`` / ``ServerSession.pending_asks``): every
  ask — whether raised by a foreground turn, a background spawn, or a
  sub-agent/lane (all share the parent's ``ToolContext.ask_fn`` closure per
  ``kernel/tools/invoke_agent.py::sub_ask_fn``) — lands in the SAME per-
  session ``pending_asks: dict[call_id, Future[bool]]``. Nothing serializes
  insertion; N concurrent asks sit in the dict simultaneously.
* Frontend (``mirror/src/stores/conversation.ts::pendingApprovals`` — a
  plain array; ``ChatView.tsx``/``HudChatInput.tsx`` render it via
  ``pendingApprovals.map(...)``): ALL pending approvals render as cards at
  once, not a one-at-a-time queue either.

So "one at a time" is not a real property of this system today. What IS
real, and what this test proves instead:

  (i)   every concurrent ask funnels onto the ONE shared ``pending_asks``
        registry (there is no per-workstream or per-lane registry) and each
        is individually addressable by its own ``call_id`` — resolving one
        never touches the others;
  (ii)  a ``steer`` arriving while N asks are pending leaves every one of
        them unresolved (extends the existing single-ask
        ``tests/parallel_tars/test_steer.py::test_steer_never_resolves_a_
        pending_ask`` regression to N concurrent asks);
  (iii) a spawn-completion note landing mid-pending-asks (the fold-back
        floor, ``ChatSession.ingest_spawn_completion``) also leaves every
        ask future unresolved — it writes to ``_pending_spawn_completions``,
        a wholly separate queue from ``pending_asks``.

Drives the REAL ``ask_gate._make_ask_fn`` machinery (same harness as
``tests/trio/test_ask_park.py`` / ``tests/parallel_tars/test_steer.py``),
not hand-staged futures, so the funnel/isolation claims are proven against
production code, not a mock of it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from pydantic import BaseModel

from tesseract.kernel.adapters.base import AdapterOptions
from tesseract.kernel.tools.base import ToolContext
from tesseract.brain.chat import ChatSession
from tesseract.mirror.server import ask_gate, turn_intake, ws as ws_mod
from tesseract.mirror.server.session import ServerSession


class _AskInput(BaseModel):
    path: str = "x.txt"


class _FakeHandle:
    """Minimal completed-spawn stand-in for ``ingest_spawn_completion``."""

    def __init__(self, handle_id: str) -> None:
        self.handle_id = handle_id
        self.kind = "delegate_claude"

    def status(self) -> str:
        return "done"


def _build_app() -> web.Application:
    app = web.Application()
    app["mood"] = None
    app["adapter_options"] = None
    app["config"] = SimpleNamespace(
        uploads=SimpleNamespace(max_files_per_message=10, max_total_mb=25),
    )
    return app


def _make_session(session_id: str = "sess-ask-funnel") -> ServerSession:
    ws = MagicMock()
    ws.send_json = AsyncMock()
    ws.closed = False
    pending_injected: list[dict] = []

    def _enqueue(text: str) -> None:
        text = (text or "").strip()
        if text:
            pending_injected.append({"text": text, "queued_at": "stub"})

    # A real ChatSession (not a bare SimpleNamespace) so the spawn-completion
    # check below (iii) exercises the actual `ingest_spawn_completion` /
    # `_pending_spawn_completions` production path, not a stand-in of it.
    chat_session = ChatSession(
        adapter=None,  # type: ignore[arg-type]
        system_prompt="",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(),
    )
    chat_session.tool_context = SimpleNamespace(cancel_event=asyncio.Event())  # type: ignore[assignment]
    chat_session.pending_injected_messages = pending_injected
    chat_session.enqueue_user_inject = _enqueue  # type: ignore[method-assign]
    return ServerSession(
        session_id=session_id,
        ws=ws,
        chat_session=chat_session,
        event_log=MagicMock(append=MagicMock()),
    )


async def _wait_until_pending(session: ServerSession, call_ids: list[str]) -> None:
    for _ in range(400):
        if all(cid in session.pending_asks for cid in call_ids):
            return
        await asyncio.sleep(0.005)
    pytest.fail(f"not all of {call_ids} registered in pending_asks in time")


async def test_three_concurrent_workstream_asks_funnel_onto_one_registry(monkeypatch):
    """(i) Two spawns + a lane turn each fire a REAL ask concurrently — all
    three land in the SAME `session.pending_asks` dict, individually keyed,
    simultaneously pending. Resolving one (out of creation order, via the
    real `ws._resolve_ask`) settles ONLY that one; the other two are
    untouched until resolved on their own."""
    monkeypatch.setattr(ask_gate, "ASK_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(ask_gate, "ASK_GRACE_SECONDS", 0.5)

    session = _make_session()
    ask_fn = ask_gate._make_ask_fn(
        session.ws, session.session_id, session.pending_asks, session.event_log,
    )
    tool = SimpleNamespace(name="file_write")
    call_ids = ["call-spawn-A", "call-spawn-B", "call-lane-C"]
    tasks = [
        asyncio.create_task(
            ask_fn(tool, _AskInput(), ToolContext(session_id=session.session_id, current_call_id=cid)),
            name=f"workstream:{cid}",
        )
        for cid in call_ids
    ]
    try:
        await _wait_until_pending(session, call_ids)

        # (i) all three pending, in ONE registry, none resolved.
        assert set(session.pending_asks.keys()) == set(call_ids)
        futures = {cid: session.pending_asks[cid] for cid in call_ids}
        assert all(not f.done() for f in futures.values())

        # Resolve the MIDDLE one first (out of creation order) via the real
        # tool_response handler — only that call_id's future settles. A brief
        # yield lets `ask_fn`'s coroutine resume past the await and run its
        # `finally: pending_asks.pop(call_id, None)` (set_result alone doesn't
        # run that — it's the awaiting task's continuation, scheduled but not
        # yet run synchronously here).
        ws_mod._resolve_ask(session, {"call_id": "call-spawn-B", "approved": True})
        assert futures["call-spawn-B"].done()
        assert not futures["call-spawn-A"].done()
        assert not futures["call-lane-C"].done()
        for _ in range(200):
            if "call-spawn-B" not in session.pending_asks:
                break
            await asyncio.sleep(0.005)
        else:
            pytest.fail("call-spawn-B never popped from pending_asks")
        assert set(session.pending_asks.keys()) == {"call-spawn-A", "call-lane-C"}

        ws_mod._resolve_ask(session, {"call_id": "call-lane-C", "approved": False})
        ws_mod._resolve_ask(session, {"call_id": "call-spawn-A", "approved": True})

        results = await asyncio.gather(*tasks)
        assert dict(zip(call_ids, results)) == {
            "call-spawn-A": True, "call-spawn-B": True, "call-lane-C": False,
        }
        assert session.pending_asks == {}
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()


async def test_steer_during_n_pending_asks_leaves_all_unresolved(monkeypatch):
    """(ii) Extends test_steer.py's single-ask safety regression to THREE
    concurrent workstream asks: a steer mid-flight must not settle any of
    them."""
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

    ask_fn = ask_gate._make_ask_fn(
        session.ws, session.session_id, session.pending_asks, session.event_log,
    )
    tool = SimpleNamespace(name="file_write")
    call_ids = ["call-spawn-A", "call-spawn-B", "call-lane-C"]
    tasks = [
        asyncio.create_task(
            ask_fn(tool, _AskInput(), ToolContext(session_id=session.session_id, current_call_id=call_id)),
        )
        for call_id in call_ids
    ]

    async def _busy():
        await asyncio.sleep(60)

    session.current_turn_task = asyncio.create_task(_busy())
    try:
        await _wait_until_pending(session, call_ids)
        futures_before = dict(session.pending_asks)

        await turn_intake.handle_steer(app, session, {"chat_id": cid, "text": "redirect mid-asks"})

        # (ii) none of the three settled by the steer.
        assert all(not f.done() for f in futures_before.values())
        assert set(session.pending_asks.keys()) == set(call_ids)
        assert session.chat_session.pending_injected_messages == [
            {"text": "redirect mid-asks", "queued_at": "stub"}
        ]
    finally:
        session.current_turn_task.cancel()
        try:
            await session.current_turn_task
        except asyncio.CancelledError:
            pass
        for cid_ in call_ids:
            fut = session.pending_asks.get(cid_)
            if fut is not None and not fut.done():
                fut.cancel()
        for t in tasks:
            if not t.done():
                t.cancel()


async def test_spawn_completion_during_n_pending_asks_leaves_all_unresolved(monkeypatch):
    """(iii) A spawn-completion fold-back note landing on the SAME chat
    session while N asks are pending must not settle any of them — the
    floor (`ingest_spawn_completion`) writes to `_pending_spawn_completions`,
    a queue `_resolve_ask` never reads from."""
    monkeypatch.setattr(ask_gate, "ASK_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(ask_gate, "ASK_GRACE_SECONDS", 0.5)

    session = _make_session()
    ask_fn = ask_gate._make_ask_fn(
        session.ws, session.session_id, session.pending_asks, session.event_log,
    )
    tool = SimpleNamespace(name="file_write")
    call_ids = ["call-spawn-A", "call-spawn-B", "call-lane-C"]
    tasks = [
        asyncio.create_task(
            ask_fn(tool, _AskInput(), ToolContext(session_id=session.session_id, current_call_id=call_id)),
        )
        for call_id in call_ids
    ]
    try:
        await _wait_until_pending(session, call_ids)
        futures = dict(session.pending_asks)

        # A THIRD, unrelated workstream finishes in the background and its
        # completion note folds back into the same chat session.
        session.chat_session.ingest_spawn_completion(_FakeHandle("del-unrelated-1"))

        assert all(not f.done() for f in futures.values())
        assert set(session.pending_asks.keys()) == set(call_ids)
        assert len(session.chat_session._pending_spawn_completions) == 1

        # Resolving the asks afterward still works normally — the note's
        # arrival didn't wedge the registry.
        for call_id in call_ids:
            ws_mod._resolve_ask(session, {"call_id": call_id, "approved": True})
        results = await asyncio.gather(*tasks)
        assert results == [True, True, True]
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
