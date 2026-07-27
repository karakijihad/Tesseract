"""P6 Task 3.1 — same-process reconnect must not declare a still-running
spawn "lost".

`spawn_journal.sweep_orphans` (fed to `ChatSession.mark_vanished_spawns` at
resume time) previously treated every journaled `start` with no matching
`terminal` as vanished, unconditionally. But a same-process reconnect (page
reload / Mirror resume against a backend that never actually restarted) can
still have the spawn's `asyncio.Task` alive in the process-global handle
registry (`spawns._ALL_HANDLES` / `find_handle`) — that handle is proof the
spawn is still running (or parked on an operator ask, trio W4's
`input_required`), not lost. Only ids `find_handle` can't vouch for
(genuinely absent, or present-but-terminal) should still surface
`[spawn_lost]`.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from tesseract.brain import failures_signal, spawn_journal
from tesseract.brain.chat import ChatSession
from tesseract.brain.spawns import SpawnRegistry, _ALL_HANDLES, mark_input_required
from tesseract.kernel.adapters.base import AdapterOptions
from tesseract.kernel.tools.base import ToolContext, ToolResult
from tesseract.kernel.tools.spawn_await import SpawnAwaitInput, SpawnAwaitTool
from tesseract.mirror.server import ask_gate as ask_gate_mod
from tesseract.mirror.server import chat_lifecycle as chat_lifecycle_mod
from tesseract.mirror.server import chat_restore, chat_store
from tesseract.mirror.server import session_factory as session_factory_mod
from tesseract.mirror.server import spawn_wake
from tesseract.mirror.server import ws as ws_mod
from tesseract.mirror.server.chat_store import ChatRecord
from tesseract.mirror.server.routes.asks_parked import decide_parked
from tesseract.mirror.server.session_cleanup import cleanup_session
from tesseract.mirror.server.session_model import ServerSession


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


async def _pending_forever(hold: asyncio.Event) -> ToolResult:
    await hold.wait()
    return ToolResult(output="done")


@pytest.mark.asyncio
async def test_live_handle_not_declared_lost_but_dead_id_still_is():
    hold = asyncio.Event()
    try:
        reg = SpawnRegistry()
        reg.session_id = "sess-reconnect"
        # Real registration API — this is what puts the handle into the
        # process-global _ALL_HANDLES the fix must consult, and also
        # journals the "start" event (SpawnRegistry.register does both).
        live = reg.register(kind="delegate_claude", coro=_pending_forever(hold))
        assert _ALL_HANDLES.get(live.handle_id) is live  # sanity: registered

        # A genuinely dead id — journaled but never registered anywhere in
        # this process (simulates the actual cross-restart case).
        spawn_journal.record_start("sess-reconnect", "h-dead", "delegate_codex", "t0")

        cs = _new_session()
        count = cs.mark_vanished_spawns("sess-reconnect")

        notes = cs._pending_spawn_completions
        assert not any(live.handle_id in n for n in notes), (
            "still-running spawn was falsely declared [spawn_lost]"
        )
        assert any("[spawn_lost]" in n and "h-dead" in n for n in notes)
        assert count == 1
    finally:
        hold.set()
        await live.task
        await asyncio.sleep(0)
        _ALL_HANDLES.pop(live.handle_id, None)


@pytest.mark.asyncio
async def test_parked_input_required_handle_not_declared_lost():
    """trio W4's `input_required` park state is ALIVE, not lost — a spawn
    waiting on an operator answer in the approvals pane must not be
    surfaced as `[spawn_lost]` just because its task hasn't completed via
    the normal path yet."""
    hold = asyncio.Event()
    try:
        reg = SpawnRegistry()
        reg.session_id = "sess-reconnect-parked"
        parked = reg.register(kind="delegate_claude", coro=_pending_forever(hold))
        mark_input_required(parked, True)

        cs = _new_session()
        count = cs.mark_vanished_spawns("sess-reconnect-parked")

        assert count == 0
        assert not any(parked.handle_id in n for n in cs._pending_spawn_completions)
    finally:
        hold.set()
        await parked.task
        await asyncio.sleep(0)
        _ALL_HANDLES.pop(parked.handle_id, None)


# --- Task 3.2: app-level spawn ownership index (M4-p2) ---------------------
#
# 3.1 stopped the resume-time sweep from declaring a still-running spawn
# lost. But even when the spawn survives, its `completion_notifier` is a
# closure pinned to the OLD session/chat (`spawn_wake.wire_chat` captures
# them at wire time) — a completion firing after reconnect notifies a dead
# ChatSession nobody reads, and one that finishes DURING the disconnected
# window notifies it and is never seen. `spawn_ownership.rebind_chat`
# (called from `chat_restore._restore_persisted_chats` right after
# `mark_vanished_spawns`) closes this: re-associates still-live spawns with
# the new chat, and replays dead-window completions into it.


async def _ok() -> ToolResult:
    return ToolResult(output="done")


class _FakeWS:
    closed = True

    async def send_json(self, env: dict) -> None:  # pragma: no cover — unused
        pass


def _server_session(session_id: str, cs: ChatSession, chat_id: str) -> ServerSession:
    return ServerSession(
        session_id=session_id,
        ws=_FakeWS(),
        chat_session=cs,
        event_log=SimpleNamespace(append=lambda _e: None),
        active_chat_id=chat_id,
    )


def _connect(app: dict, session: ServerSession) -> None:
    app.setdefault("sessions", {})[session.session_id] = session.ws
    app.setdefault("server_sessions", {})[session.session_id] = session
    app.setdefault("event_logs", {})[session.session_id] = session.event_log


def _save_chat(chat_id: str, session_id: str, title: str) -> None:
    chat_store.save_chat(ChatRecord(
        chat_id=chat_id, session_id=session_id, title=title,
        created_at="2026-07-11T10:00:00", started_at="2026-07-11T10:00:00",
    ))


def _stub_new_chat_session(monkeypatch: pytest.MonkeyPatch) -> None:
    # `_restore_persisted_chats` resolves `new_chat_session` from
    # `session_factory` at call time; the real builder needs full boot infra
    # (`app["adapter_entry"]` etc.) — same seam existing chat-restore tests
    # (`tests/mirror/test_chat_ws_handlers.py`) patch around.
    def _fake(app_, sess, **kw):
        return _new_session(tool_context=ToolContext(session_id=sess.session_id))

    monkeypatch.setattr(session_factory_mod, "new_chat_session", _fake)


@pytest.mark.asyncio
async def test_rebind_chat_redirects_live_spawn_completion_to_new_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spawn still running at reconnect must notify the NEW chat's
    ChatSession when it later completes, not the orphaned old one — and the
    new chat must be able to `spawn_await` it."""
    app: dict = {}
    chat_id = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"

    cs_a = _new_session(tool_context=ToolContext(session_id="sess-a"))
    session_a = _server_session("sess-a", cs_a, chat_id)
    _connect(app, session_a)
    spawn_wake.wire_chat(app, session_a, chat_id, cs_a)

    hold = asyncio.Event()
    handle = cs_a.spawns.register(kind="delegate_claude", coro=_pending_forever(hold))
    _save_chat(chat_id, "sess-a", "X")

    try:
        cleanup_session(app, session_a)
        assert "sess-a" not in app["server_sessions"]

        seed_cs = _new_session()
        session_b = _server_session("sess-b", seed_cs, "")
        _connect(app, session_b)
        _stub_new_chat_session(monkeypatch)

        chat_restore._restore_persisted_chats(app, session_b)
        new_cs = session_b.chats[chat_id]
        # The Stage-2 idle-wake proactive turn is orthogonal to this test —
        # suppress it so `on_spawn_complete` stops at the floor.
        session_b.spawn_wake_pending.add(chat_id)

        hold.set()
        await handle.task
        await asyncio.sleep(0)  # let the (rebound) done-callback run

        assert len(new_cs._pending_spawn_completions) == 1
        assert handle.handle_id in new_cs._pending_spawn_completions[0]
        assert len(cs_a._pending_spawn_completions) == 0  # orphan never notified

        tool = SpawnAwaitTool()
        ctx = ToolContext(session_id="sess-b", spawns=new_cs.spawns)
        result = await tool.run(SpawnAwaitInput(handle=handle.handle_id), ctx)
        assert not result.is_error
        assert result.output == "done"
    finally:
        hold.set()
        if not handle.task.done():
            await handle.task
        await asyncio.sleep(0)
        _ALL_HANDLES.pop(handle.handle_id, None)


@pytest.mark.asyncio
async def test_rebind_chat_replays_completion_finished_during_dead_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spawn that finishes WHILE disconnected notifies the orphaned old
    ChatSession (unavoidable — its notifier already fired). Reconnect must
    replay that completion into the new chat so the result is observable,
    and finalize the ownership bookkeeping so it isn't replayed again."""
    app: dict = {}
    chat_id = "b1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"

    cs_a = _new_session(tool_context=ToolContext(session_id="sess-a2"))
    session_a = _server_session("sess-a2", cs_a, chat_id)
    _connect(app, session_a)
    spawn_wake.wire_chat(app, session_a, chat_id, cs_a)
    # Suppress the Stage-2 idle-wake proactive turn (orthogonal to this
    # test) so the floor delivery below isn't drained by a real wake-turn
    # attempt that has no adapter to run against.
    session_a.spawn_wake_pending.add(chat_id)

    hold = asyncio.Event()
    handle = cs_a.spawns.register(kind="delegate_claude", coro=_pending_forever(hold))
    _save_chat(chat_id, "sess-a2", "Y")

    try:
        cleanup_session(app, session_a)

        # Completes WHILE disconnected — its own (stale) notifier still
        # points at the now-orphaned cs_a.
        hold.set()
        await handle.task
        await asyncio.sleep(0)

        assert len(cs_a._pending_spawn_completions) == 1  # landed in the dead object
        index = app["spawn_ownership"]
        assert handle.handle_id in index.chat_handles.get(chat_id, [])  # not finalized

        seed_cs = _new_session()
        session_b = _server_session("sess-b2", seed_cs, "")
        _connect(app, session_b)
        _stub_new_chat_session(monkeypatch)

        chat_restore._restore_persisted_chats(app, session_b)
        new_cs = session_b.chats[chat_id]

        assert len(new_cs._pending_spawn_completions) == 1  # replayed
        assert handle.handle_id in new_cs._pending_spawn_completions[0]
        assert handle.handle_id not in index.chat_handles.get(chat_id, [])  # cleaned up
        assert handle.handle_id not in index.bindings
    finally:
        _ALL_HANDLES.pop(handle.handle_id, None)


@pytest.mark.asyncio
async def test_rebind_chat_is_idempotent_across_two_reconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconnecting twice while the spawn is still running must not double-
    fire the completion into either intermediate chat — only the LATEST
    chat should ever see it."""
    app: dict = {}
    chat_id = "c1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"

    cs_a = _new_session(tool_context=ToolContext(session_id="sess-a3"))
    session_a = _server_session("sess-a3", cs_a, chat_id)
    _connect(app, session_a)
    spawn_wake.wire_chat(app, session_a, chat_id, cs_a)

    hold = asyncio.Event()
    handle = cs_a.spawns.register(kind="delegate_claude", coro=_pending_forever(hold))
    _save_chat(chat_id, "sess-a3", "Z")

    try:
        cleanup_session(app, session_a)
        _stub_new_chat_session(monkeypatch)

        seed_cs_b = _new_session()
        session_b = _server_session("sess-b3", seed_cs_b, "")
        _connect(app, session_b)
        chat_restore._restore_persisted_chats(app, session_b)
        cs_b = session_b.chats[chat_id]
        session_b.spawn_wake_pending.add(chat_id)
        cleanup_session(app, session_b)

        seed_cs_c = _new_session()
        session_c = _server_session("sess-c3", seed_cs_c, "")
        _connect(app, session_c)
        chat_restore._restore_persisted_chats(app, session_c)
        cs_c = session_c.chats[chat_id]
        session_c.spawn_wake_pending.add(chat_id)

        hold.set()
        await handle.task
        await asyncio.sleep(0)

        assert len(cs_a._pending_spawn_completions) == 0
        assert len(cs_b._pending_spawn_completions) == 0
        assert len(cs_c._pending_spawn_completions) == 1
        assert handle.handle_id in cs_c._pending_spawn_completions[0]
    finally:
        hold.set()
        if not handle.task.done():
            await handle.task
        await asyncio.sleep(0)
        _ALL_HANDLES.pop(handle.handle_id, None)


# --- 3.2 review fix-pass: the OTHER cross-session rebuild path -------------
#
# `chat_restore._restore_persisted_chats` (full-session reconnect) calls
# `rebind_chat` right after `mark_vanished_spawns`. `ws._handle_chat_restore`'s
# "PRIOR session" branch (un-archive via the `chat.restore` WS message)
# rebuilds a ChatSession from the same kind of persisted record and already
# calls `mark_vanished_spawns` + `spawn_wake.wire_chat` — but was missing the
# matching `rebind_chat` call, so a dead-window completion for a chat archived
# then un-archived via `chat.restore` never reached the new ChatSession and
# the ownership index entry leaked forever.


@pytest.mark.asyncio
async def test_chat_restore_ws_handler_rebinds_dead_window_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive `ws._handle_chat_restore`'s persisted-record rebuild branch (not
    `chat_restore._restore_persisted_chats`) and assert the dead-window
    completion is replayed into the NEW chat session and the ownership index
    entry is finalized (bindings cleaned)."""
    app: dict = {}
    chat_id = "d1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"

    cs_a = _new_session(tool_context=ToolContext(session_id="sess-a4"))
    session_a = _server_session("sess-a4", cs_a, chat_id)
    _connect(app, session_a)
    spawn_wake.wire_chat(app, session_a, chat_id, cs_a)
    # Suppress the Stage-2 idle-wake proactive turn (orthogonal to this test).
    session_a.spawn_wake_pending.add(chat_id)

    hold = asyncio.Event()
    handle = cs_a.spawns.register(kind="delegate_claude", coro=_pending_forever(hold))
    _save_chat(chat_id, "sess-a4", "W")
    chat_store.set_archived(chat_id, True)  # chat.restore only rebuilds archived records

    try:
        cleanup_session(app, session_a)

        # Completes WHILE archived/disconnected — its own (stale) notifier
        # still points at the now-orphaned cs_a.
        hold.set()
        await handle.task
        await asyncio.sleep(0)

        assert len(cs_a._pending_spawn_completions) == 1  # landed in the dead object
        index = app["spawn_ownership"]
        assert handle.handle_id in index.chat_handles.get(chat_id, [])  # not finalized

        seed_cs = _new_session()
        session_b = _server_session("sess-b4", seed_cs, "")
        _connect(app, session_b)
        # `_handle_chat_restore` (SDD Task 7.2: moved to chat_lifecycle.py)
        # resolves `new_chat_session` from its own module globals — patching
        # `ws_mod` here would be a dead patch (audited on move; break-to-prove
        # confirmed the fake never fired against `ws_mod`).
        monkeypatch.setattr(
            chat_lifecycle_mod, "new_chat_session",
            lambda app_, sess, **kw: _new_session(tool_context=ToolContext(session_id=sess.session_id)),
        )

        await ws_mod._handle_chat_restore(app, session_b, {"chat_id": chat_id})

        new_cs = session_b.chats[chat_id]
        assert len(new_cs._pending_spawn_completions) == 1  # replayed
        assert handle.handle_id in new_cs._pending_spawn_completions[0]
        assert handle.handle_id not in index.chat_handles.get(chat_id, [])  # cleaned up
        assert handle.handle_id not in index.bindings
    finally:
        _ALL_HANDLES.pop(handle.handle_id, None)


# --- Task 3.3: full disconnect -> reconnect -> REST-decide -> resume ------
#
# Ties Task 3.1 (a parked spawn must not be swept as `[spawn_lost]`) and
# Task 3.2 (`spawn_ownership.rebind_chat` re-targets the completion to the
# reconnected chat) together with the trio W4 ask-park machinery
# (`ask_gate._make_ask_fn`/`_park_and_wait`) and the REAL REST decision
# surface (`routes/asks_parked.decide_parked`) — the full operator story:
# "my background ask survived a reload and my late answer resumed it, and I
# saw the result in the chat I reconnected to."


class _AskFileWrite(BaseModel):
    path: str = "e2e.txt"


class _FakeDecisionRequest:
    """Minimal aiohttp-request stand-in for `decide_parked` — same shape
    `tests/trio/test_asks_parked_routes.py` drives the route with."""

    def __init__(self, app: dict, approval_id: str, approved: bool) -> None:
        self.app = app
        self.match_info = {"approval_id": approval_id}
        self._body = {"approved": approved}

    async def json(self):
        return self._body


def _fast_ask_windows(monkeypatch: pytest.MonkeyPatch, park_timeout: float = 5.0) -> None:
    # Same seam `tests/trio/test_ask_park.py::_fast_windows` patches —
    # `_make_ask_fn`'s body resolves these names against ask_gate's own
    # globals, not session.py's re-exported copies (a dead patch otherwise).
    from tesseract.config import runtime_limits

    monkeypatch.setattr(ask_gate_mod, "ASK_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(ask_gate_mod, "ASK_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(runtime_limits, "load_ask_park_timeout_s", lambda p: park_timeout)


@pytest.mark.asyncio
async def test_full_reconnect_lifecycle_disconnect_park_decide_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) a background spawn's ASK parks; (b) the operator disconnects
    before deciding; (c) reconnect rebuilds chat X from disk; (d) the
    operator decides the parked ask via the real REST route; (e) the
    resumed spawn completes; (f) the completion is observed in the NEW
    chat X, never the orphaned original."""
    _fast_ask_windows(monkeypatch)
    app: dict = {}
    chat_id = "e1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
    session_id = "sess-e2e-a"
    call_id = "call-e2e-1"

    cs_a = _new_session(tool_context=ToolContext(session_id=session_id))
    session_a = _server_session(session_id, cs_a, chat_id)
    app.setdefault("parked_asks", {})
    session_a.parked_asks = app["parked_asks"]  # production shares this dict app-wide
    _connect(app, session_a)
    spawn_wake.wire_chat(app, session_a, chat_id, cs_a)
    _save_chat(chat_id, session_id, "E2E")

    ask_fn = ask_gate_mod._make_ask_fn(
        session_a.ws, session_id, session_a.pending_asks, session_a.event_log,
        session_a.parked_asks,
    )
    tool = SimpleNamespace(name="file_write")

    async def _work() -> ToolResult:
        ctx = ToolContext(session_id=session_id, current_call_id=call_id)
        ok = await ask_fn(tool, _AskFileWrite(), ctx)
        return ToolResult(output=f"approved={ok}")

    # (a) background spawn under session A / chat X hits an ASK that parks.
    handle = cs_a.spawns.register(kind="delegate_claude", coro=_work())
    try:
        for _ in range(400):
            if any(e.call_id == call_id for e in app["parked_asks"].values()):
                break
            await asyncio.sleep(0.005)
        else:
            pytest.fail("ask never parked")
        assert handle.status() == "input_required"

        # (b) session A disconnects BEFORE deciding.
        cleanup_session(app, session_a)
        assert session_id not in app["server_sessions"]
        parked_entry = next(
            e for e in app["parked_asks"].values() if e.call_id == call_id
        )
        assert not parked_entry.future.done(), "cleanup force-denied a parked ask"

        # (c) reconnect: rebuild chat X from disk under a NEW session/chat.
        seed_cs = _new_session()
        session_b = _server_session("sess-e2e-b", seed_cs, "")
        _connect(app, session_b)
        _stub_new_chat_session(monkeypatch)
        chat_restore._restore_persisted_chats(app, session_b)
        new_cs = session_b.chats[chat_id]
        session_b.spawn_wake_pending.add(chat_id)  # suppress orthogonal idle-wake turn
        assert handle.handle_id in app["spawn_ownership"].bindings  # rebound, not dropped

        # (d) decide the parked ask via the REAL REST route.
        resp = await decide_parked(
            _FakeDecisionRequest(app, parked_entry.approval_id, True)
        )
        assert resp.status == 200

        # (e) the resumed spawn completes.
        result = await handle.task
        assert result.output == "approved=True"
        await asyncio.sleep(0)  # let the (rebound) done-callback run

        # (f) observed in the NEW chat X, not the orphaned original.
        assert len(new_cs._pending_spawn_completions) == 1
        assert handle.handle_id in new_cs._pending_spawn_completions[0]
        assert len(cs_a._pending_spawn_completions) == 0
    finally:
        if not handle.task.done():
            handle.task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await handle.task
        await asyncio.sleep(0)
        _ALL_HANDLES.pop(handle.handle_id, None)
        app["parked_asks"].clear()
