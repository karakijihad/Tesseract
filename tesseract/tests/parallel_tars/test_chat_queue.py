"""conversation-layer Task 4.1 — runtime.yaml::chat_queue_max loader.

Per CLAUDE.md "no hardcoded defaults for infrastructure values; raise loudly on
missing keys" — the chat-queue depth cap must raise on missing/invalid values,
never silently fall back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.config.runtime_limits import load_chat_queue_max


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_loader_returns_int_when_key_present(tmp_path: Path) -> None:
    p = tmp_path / "runtime.yaml"
    _write(p, "chat_queue_max: 10\n")
    assert load_chat_queue_max(p) == 10


def test_loader_raises_when_key_missing(tmp_path: Path) -> None:
    p = tmp_path / "runtime.yaml"
    _write(p, "spawn_stall_seconds: 900\n")
    with pytest.raises(ValueError, match="missing 'chat_queue_max'"):
        load_chat_queue_max(p)


def test_loader_rejects_non_int(tmp_path: Path) -> None:
    p = tmp_path / "runtime.yaml"
    _write(p, "chat_queue_max: nope\n")
    with pytest.raises(ValueError, match="must be int"):
        load_chat_queue_max(p)


def test_loader_rejects_below_one(tmp_path: Path) -> None:
    p = tmp_path / "runtime.yaml"
    _write(p, "chat_queue_max: 0\n")
    with pytest.raises(ValueError, match="must be >=1"):
        load_chat_queue_max(p)


def test_default_runtime_config_has_the_key() -> None:
    from tesseract.config.runtime_limits import default_runtime_config_path

    assert load_chat_queue_max(default_runtime_config_path()) >= 1


# ── Task 4.2 (Q2) — per-chat FIFO queue behavior ──────────────────────

import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web

from tesseract.config import runtime_limits
from tesseract.mirror.server import turn_runner as turn_runner_module
from tesseract.mirror.server import ws as ws_module
from tesseract.mirror.server.session import ServerSession


def _build_app() -> web.Application:
    app = web.Application()
    app["mood"] = None
    app["adapter_options"] = None
    # `_validated_attachments` re-runs on drain re-entry (the queued payload
    # carries an already-validated `[]`, which still touches `app["config"]
    # .uploads` before short-circuiting on the empty list).
    app["config"] = SimpleNamespace(
        uploads=SimpleNamespace(max_files_per_message=10, max_total_mb=25),
    )
    return app


def _make_session(session_id: str = "sess-q2") -> ServerSession:
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


async def test_queue_appends_in_fifo_order_with_envelope_shape(monkeypatch):
    """Q2: while a turn is active, plain-text follow-ups append to
    `chat_queues[chat_id]` in arrival order (no last-wins coalescing) and
    each emits `queued_message` with the post-append `queue_size` and this
    entry's 1-based FIFO `position`. No mid-turn inject — Q2 unifies plain
    text onto the FIFO queue as a normal turn; inject is reserved for the
    future Q3 steer command."""
    monkeypatch.setattr(runtime_limits, "load_chat_queue_max", lambda p: 10)
    app = _build_app()
    session = _make_session()
    cid = session.active_chat_id

    sent: list[dict] = []

    async def fake_send(sess, env):
        if env is not None:
            sent.append(env)

    monkeypatch.setattr("tesseract.mirror.server.turn_intake.send_envelope", fake_send)

    async def _busy():
        await asyncio.sleep(60)

    session.current_turn_task = asyncio.create_task(_busy())
    try:
        await ws_module._start_turn(app, session, {"text": "first"})
        await ws_module._start_turn(app, session, {"text": "second"})
        await ws_module._start_turn(app, session, {"text": "third"})

        queue = session.chat_queues[cid]
        assert [e["text"] for e in queue] == ["first", "second", "third"]

        overflow_envs = [e for e in sent if e["type"] == "chat_queue_overflow"]
        assert overflow_envs == []
        queued_envs = [e for e in sent if e["type"] == "queued_message"]
        assert len(queued_envs) == 3
        assert [e["data"]["queue_size"] for e in queued_envs] == [1, 2, 3]
        assert [e["data"]["position"] for e in queued_envs] == [1, 2, 3]
        assert session.chat_session.pending_injected_messages == []
    finally:
        session.current_turn_task.cancel()
        try:
            await session.current_turn_task
        except asyncio.CancelledError:
            pass


async def test_overflow_drops_message_and_emits_chat_queue_overflow(monkeypatch):
    """Q2: a follow-up arriving when the queue is already at
    `chat_queue_max` is DROPPED (never silently) and a `chat_queue_overflow`
    envelope fires instead of `queued_message`. Existing queued entries are
    untouched."""
    monkeypatch.setattr(runtime_limits, "load_chat_queue_max", lambda p: 3)
    app = _build_app()
    session = _make_session()
    cid = session.active_chat_id

    sent: list[dict] = []

    async def fake_send(sess, env):
        if env is not None:
            sent.append(env)

    monkeypatch.setattr("tesseract.mirror.server.turn_intake.send_envelope", fake_send)

    async def _busy():
        await asyncio.sleep(60)

    session.current_turn_task = asyncio.create_task(_busy())
    try:
        await ws_module._start_turn(app, session, {"text": "first"})
        await ws_module._start_turn(app, session, {"text": "second"})
        await ws_module._start_turn(app, session, {"text": "third"})
        await ws_module._start_turn(app, session, {"text": "fourth"})  # over cap

        queue = session.chat_queues[cid]
        assert [e["text"] for e in queue] == ["first", "second", "third"]

        overflow_envs = [e for e in sent if e["type"] == "chat_queue_overflow"]
        assert len(overflow_envs) == 1
        assert overflow_envs[0]["data"]["text"] == "fourth"
        assert overflow_envs[0]["data"]["queue_size"] == 3
    finally:
        session.current_turn_task.cancel()
        try:
            await session.current_turn_task
        except asyncio.CancelledError:
            pass


async def test_cancel_turn_clears_whole_chat_queue():
    """Q2: `_cancel_turn` drops the ENTIRE queue for the active chat, not
    just one entry — generalizes the old single-slot cancel contract to N
    queued entries."""
    session = _make_session()
    cid = session.active_chat_id
    session.chat_queues[cid] = deque([
        {"text": "a", "attachments": [], "view_snapshot": None},
        {"text": "b", "attachments": [], "view_snapshot": None},
    ])
    session.current_turn_task = None
    app = _build_app()
    await ws_module._cancel_turn(app, session)
    assert cid not in session.chat_queues


async def test_sequential_turns_drain_fifo_no_interleave(monkeypatch):
    """Q2 end-to-end: while turn 1 is genuinely running (real `_run_turn` /
    `drain_next` wiring), three follow-ups queue; each drains ONE AT A TIME
    as the prior turn completes — proven by requiring every entry to
    `enter` before the next is even spawned."""
    monkeypatch.setattr(runtime_limits, "load_chat_queue_max", lambda p: 10)
    app = _build_app()
    session = _make_session()
    cid = session.active_chat_id

    entered: list[str] = []
    gates: dict[str, asyncio.Event] = {}

    class _CS:
        history: list = []

        def __init__(self) -> None:
            self.pending_injected_messages: list = []

        async def send(self, content, **kwargs):
            text = content if isinstance(content, str) else str(content)
            entered.append(text)
            gate = gates.setdefault(text, asyncio.Event())
            await gate.wait()
            return
            yield  # pragma: no cover — makes this an async generator

    cs = _CS()
    session.chat_session = cs
    session.chats[cid] = cs

    async def _settle(n: int) -> None:
        for _ in range(200):
            if len(entered) >= n:
                return
            await asyncio.sleep(0)
        raise AssertionError(f"turn for entry {n} never started; entered={entered}")

    with (
        patch.object(turn_runner_module, "_maybe_auto_compact", new=AsyncMock(return_value=None)),
        patch.object(turn_runner_module, "emit_stats", new=AsyncMock(return_value=None)),
    ):
        await ws_module._start_turn(app, session, {"text": "t0"})
        await _settle(1)
        assert entered == ["t0"]

        await ws_module._start_turn(app, session, {"text": "t1"})
        await ws_module._start_turn(app, session, {"text": "t2"})
        await ws_module._start_turn(app, session, {"text": "t3"})
        assert [e["text"] for e in session.chat_queues[cid]] == ["t1", "t2", "t3"]
        assert entered == ["t0"]  # nothing started early — still queued only

        turn0_task = session.current_turn_task
        gates["t0"].set()
        await turn0_task
        await _settle(2)
        assert entered == ["t0", "t1"]
        assert [e["text"] for e in session.chat_queues[cid]] == ["t2", "t3"]

        turn1_task = session.current_turn_task
        gates["t1"].set()
        await turn1_task
        await _settle(3)
        assert entered == ["t0", "t1", "t2"]
        assert [e["text"] for e in session.chat_queues[cid]] == ["t3"]

        turn2_task = session.current_turn_task
        gates["t2"].set()
        await turn2_task
        await _settle(4)
        assert entered == ["t0", "t1", "t2", "t3"]
        assert not session.chat_queues.get(cid)

        turn3_task = session.current_turn_task
        gates["t3"].set()
        await turn3_task


async def test_turn_crash_mid_queue_survives_and_drains_on_next_opportunity(monkeypatch):
    """Q2: a genuine mid-turn crash (uncaught exception, NOT an operator
    cancel) must not strand the queue. `_run_turn`'s finally-tail still
    drains — only an explicit cancel (`_cancel_turn`, which already clears
    the queue itself) skips it."""
    monkeypatch.setattr(runtime_limits, "load_chat_queue_max", lambda p: 10)
    app = _build_app()
    session = _make_session()
    cid = session.active_chat_id

    entered: list[str] = []
    gates: dict[str, asyncio.Event] = {}

    class _CS:
        history: list = []

        def __init__(self) -> None:
            self.pending_injected_messages: list = []

        async def send(self, content, **kwargs):
            text = content if isinstance(content, str) else str(content)
            entered.append(text)
            gate = gates.setdefault(text, asyncio.Event())
            await gate.wait()
            if text == "t0":
                raise RuntimeError("boom")
            return
            yield  # pragma: no cover — makes this an async generator

    cs = _CS()
    session.chat_session = cs
    session.chats[cid] = cs

    async def _settle(n: int) -> None:
        for _ in range(200):
            if len(entered) >= n:
                return
            await asyncio.sleep(0)
        raise AssertionError(f"turn for entry {n} never started; entered={entered}")

    with (
        patch.object(turn_runner_module, "_maybe_auto_compact", new=AsyncMock(return_value=None)),
        patch.object(turn_runner_module, "emit_stats", new=AsyncMock(return_value=None)),
    ):
        await ws_module._start_turn(app, session, {"text": "t0"})
        await _settle(1)

        await ws_module._start_turn(app, session, {"text": "t1"})
        await ws_module._start_turn(app, session, {"text": "t2"})
        assert [e["text"] for e in session.chat_queues[cid]] == ["t1", "t2"]

        turn0_task = session.current_turn_task
        gates["t0"].set()
        await turn0_task  # _run_turn swallows the RuntimeError into stream_error

        # The crash must NOT strand the remaining queue — drain still ran.
        await _settle(2)
        assert entered == ["t0", "t1"]
        assert [e["text"] for e in session.chat_queues[cid]] == ["t2"]

        turn1_task = session.current_turn_task
        gates["t1"].set()
        await turn1_task
        await _settle(3)
        assert entered == ["t0", "t1", "t2"]
        assert not session.chat_queues.get(cid)

        turn2_task = session.current_turn_task
        gates["t2"].set()
        await turn2_task
