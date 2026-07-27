"""W4 — ask-instead-of-die: a background spawn's unattended ASK parks
(`input_required`) instead of hard-denying at 30s; the operator's decision
— whenever it lands — settles the same future and the work resumes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from tesseract.brain.spawns import SpawnRegistry, find_handle
from tesseract.config import runtime_limits
from tesseract.kernel.tools.base import ToolContext, ToolResult
from tesseract.mirror.server import ask_gate as ask_gate_mod
from tesseract.mirror.server import session as session_mod


class _AskInput(BaseModel):
    path: str = "x.txt"


class _FakeWS:
    def __init__(self) -> None:
        self.closed = False
        self.fail_send = False  # simulate a broken socket that raises on send
        self.sent: list[dict] = []

    async def send_json(self, env: dict) -> None:
        if self.fail_send:
            raise ConnectionResetError("socket gone")
        self.sent.append(env)


class _FakeEventLog:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    def append(self, env: dict) -> None:
        self.entries.append(env)


def _fast_windows(monkeypatch, park_timeout: float = 5.0):
    # `_make_ask_fn` is defined in ask_gate.py — its body resolves these two
    # names against ask_gate's globals, not session.py's re-exported copies.
    # Patching session_mod here would be a dead patch (real 30s/1.5s windows
    # would run underneath the test).
    monkeypatch.setattr(ask_gate_mod, "ASK_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(ask_gate_mod, "ASK_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(
        runtime_limits, "load_ask_park_timeout_s", lambda p: park_timeout
    )


def _harness(parked: dict):
    ws = _FakeWS()
    events = _FakeEventLog()
    pending: dict = {}
    ask_fn = session_mod._make_ask_fn(ws, "s-trio-w4", pending, events, parked)
    tool = SimpleNamespace(name="file_write")
    ctx = ToolContext(session_id="s-trio-w4", current_call_id="call-park-1")
    return ws, events, ask_fn, tool, ctx


def _env_types(events: _FakeEventLog) -> list[str]:
    return [e.get("type") for e in events.entries]


def test_background_ask_parks_then_resumes_on_approval(isolated_home, monkeypatch):
    async def _run():
        _fast_windows(monkeypatch)
        parked: dict = {}
        ws, events, ask_fn, tool, ctx = _harness(parked)
        registry = SpawnRegistry()

        async def _work() -> ToolResult:
            ok = await ask_fn(tool, _AskInput(), ctx)
            return ToolResult(output=f"approved={ok}")

        handle = registry.register(kind="delegate_claude", coro=_work())
        for _ in range(400):
            if any(e.call_id == "call-park-1" for e in parked.values()):
                break
            await asyncio.sleep(0.005)
        else:
            pytest.fail("ask never parked")

        assert handle.status() == "input_required"
        assert find_handle(handle.handle_id) is handle
        assert "tool_ask_parked" in _env_types(events)

        # Operator decides via the approvals surface (same future).
        _entry = next(e for e in parked.values() if e.call_id == "call-park-1")
        _entry.future.set_result(True)
        result = await handle.task
        assert result.output == "approved=True"
        assert handle.status() == "done"
        assert parked == {}
        assert "tool_approved" in _env_types(events)

    asyncio.run(_run())


def test_disconnect_before_park_still_parks(isolated_home, monkeypatch):
    # M4 — the originating socket breaks when the ask fires (send raises). The
    # initial send must fail-soft (not propagate) so a background-spawn ask
    # still reaches its parking path instead of hard-denying the spawn. Without
    # the try/except, the raised send would abort the spawn coroutine and this
    # test's `await handle.task` would re-raise.
    async def _run():
        _fast_windows(monkeypatch)
        parked: dict = {}
        ws, events, ask_fn, tool, ctx = _harness(parked)
        ws.fail_send = True  # socket appears open but send raises (the race)
        registry = SpawnRegistry()

        async def _work() -> ToolResult:
            ok = await ask_fn(tool, _AskInput(), ctx)
            return ToolResult(output=f"approved={ok}")

        handle = registry.register(kind="delegate_claude", coro=_work())
        for _ in range(400):
            if any(e.call_id == "call-park-1" for e in parked.values()):
                break
            await asyncio.sleep(0.005)
        else:
            pytest.fail("ask did not park despite a closed socket (M4 fail-soft)")

        # Operator settles it later via the approvals surface — work resumes.
        _entry = next(e for e in parked.values() if e.call_id == "call-park-1")
        _entry.future.set_result(True)
        result = await handle.task
        assert result.output == "approved=True"

    asyncio.run(_run())


def test_park_timeout_finally_denies(isolated_home, monkeypatch):
    async def _run():
        _fast_windows(monkeypatch, park_timeout=0.05)
        parked: dict = {}
        ws, events, ask_fn, tool, ctx = _harness(parked)
        registry = SpawnRegistry()

        async def _work() -> ToolResult:
            ok = await ask_fn(tool, _AskInput(), ctx)
            return ToolResult(output=f"approved={ok}")

        handle = registry.register(kind="delegate_codex", coro=_work())
        result = await handle.task
        assert result.output == "approved=False"
        assert handle.input_required is False
        assert parked == {}
        types = _env_types(events)
        assert "tool_ask_parked" in types
        assert "tool_denied" in types

    asyncio.run(_run())


def test_foreground_ask_timeout_unchanged(isolated_home, monkeypatch):
    """A NON-spawn (foreground turn) ask must deny at the window exactly as
    before — no park entry, no parked envelope."""

    async def _run():
        _fast_windows(monkeypatch)
        parked: dict = {}
        ws, events, ask_fn, tool, ctx = _harness(parked)

        approved = await ask_fn(tool, _AskInput(), ctx)
        assert approved is False
        assert parked == {}
        types = _env_types(events)
        assert "tool_ask_parked" not in types
        assert "tool_denied" in types

    asyncio.run(_run())


def test_operator_decline_while_parked(isolated_home, monkeypatch):
    async def _run():
        _fast_windows(monkeypatch)
        parked: dict = {}
        ws, events, ask_fn, tool, ctx = _harness(parked)
        registry = SpawnRegistry()

        async def _work() -> ToolResult:
            ok = await ask_fn(tool, _AskInput(), ctx)
            return ToolResult(output=f"approved={ok}")

        handle = registry.register(kind="delegate_claude", coro=_work())
        for _ in range(400):
            if any(e.call_id == "call-park-1" for e in parked.values()):
                break
            await asyncio.sleep(0.005)
        _entry = next(e for e in parked.values() if e.call_id == "call-park-1")
        _entry.future.set_result(False)
        result = await handle.task
        assert result.output == "approved=False"
        assert "tool_denied" in _env_types(events)

    asyncio.run(_run())


def test_sweep_stalled_skips_parked_spawn(isolated_home):
    async def _run():
        from datetime import datetime, timedelta, timezone

        registry = SpawnRegistry()

        async def _never() -> ToolResult:
            await asyncio.Event().wait()
            return ToolResult(output="unreachable")

        handle = registry.register(kind="delegate_claude", coro=_never())
        handle.input_required = True
        future_ref = datetime.now(timezone.utc) + timedelta(hours=2)
        assert registry.sweep_stalled(max_age_seconds=1, now=future_ref) == []
        handle.input_required = False
        stalled = registry.sweep_stalled(max_age_seconds=1, now=future_ref)
        assert [h.handle_id for h in stalled] == [handle.handle_id]
        await registry.cancel_all()

    asyncio.run(_run())


def test_cleanup_session_preserves_parked_futures(isolated_home):
    """Regression (W4 review, critical): a WS disconnect force-denied every
    pending ask, including PARKED ones — killing the parked spawn on every
    tab close. Parked futures must survive cleanup_session."""

    async def _run():
        loop = asyncio.get_running_loop()
        fut_parked = loop.create_future()
        fut_plain = loop.create_future()
        entry = session_mod.ParkedAsk(
            call_id="parked-1",
            session_id="s-1",
            tool_name="file_write",
            input_summary="x",
            spawn_handle_id="del-1",
            parked_at="2026-07-10T00:00:00+00:00",
            future=fut_parked,
        )
        app = {
            "sessions": {},
            "server_sessions": {},
            "event_logs": {},
            "parked_asks": {"parked-1": entry},
        }
        session = SimpleNamespace(
            session_id="s-1",
            turn_count=0,
            current_turn_tasks={},
            turn_states_by_chat={},
            tts_synth_task=None,
            pending_overage_asks={},
            pending_asks={"parked-1": fut_parked, "plain-1": fut_plain},
            voice_pcm_buffer=None,
        )
        session_mod.cleanup_session(app, session)
        assert not fut_parked.done(), "parked ask was force-denied on disconnect"
        assert fut_plain.done() and fut_plain.result() is False
        assert "parked-1" in app["parked_asks"]

    asyncio.run(_run())


def test_detection_parses_piped_fanout_task_name():
    """`spawn:<id>|tool:…` (concurrency-safe fan-out inheritance) resolves
    to the spawn handle id."""

    async def _run():
        async def _probe():
            return session_mod._spawn_handle_id_of_current_task()

        task = asyncio.create_task(_probe(), name="spawn:del-9|tool:web_search:1")
        assert await task == "del-9"
        plain = asyncio.create_task(_probe(), name="tool:web_search:2")
        assert await plain is None

    asyncio.run(_run())


def test_fanout_tool_task_inherits_spawn_prefix(monkeypatch):
    """Regression (W4 review, major): a concurrency-safe ASK tool runs in a
    fan-out task — its name must carry the parent spawn prefix or the ask
    hard-denies instead of parking."""

    async def _run():
        from tesseract.brain import chat as chat_mod
        from tesseract.kernel.state import ToolCall

        seen: list[str] = []

        async def _fake_execute_tool(**kwargs):
            task = asyncio.current_task()
            seen.append(task.get_name() if task else "")
            return ToolResult(output="ok")

        monkeypatch.setattr(chat_mod, "execute_tool", _fake_execute_tool)

        class _SafeTool:
            def is_concurrency_safe(self) -> bool:
                return True

        class _Registry:
            def get(self, name):
                return _SafeTool()

        session = chat_mod.ChatSession(
            adapter=object(),
            system_prompt="",
            max_tool_iterations=5,
            max_consecutive_adapter_errors=3,
            registry=_Registry(),
        )

        async def _drain():
            calls = [ToolCall(id="tc-1", name="web_search", input={})]
            async for _chunk in session._run_pending_calls(calls):
                pass

        task = asyncio.create_task(_drain(), name="spawn:del-42")
        await task
        assert len(seen) == 1
        assert seen[0].startswith("spawn:del-42|tool:web_search:")

    asyncio.run(_run())


def test_input_required_projects_to_activity_state():
    from tesseract.orchestrator.activity.models import ActivityState
    from typing import get_args

    assert "input_required" in get_args(ActivityState)


def test_loader_reads_repo_config_and_rejects_missing_key(tmp_path):
    from tesseract.config.runtime_limits import (
        default_runtime_config_path,
        load_ask_park_timeout_s,
    )

    assert load_ask_park_timeout_s(default_runtime_config_path()) > 0
    stub = tmp_path / "runtime.yaml"
    stub.write_text("spawn_stall_seconds: 900\n", encoding="utf-8")
    with pytest.raises(ValueError, match="ask_park_timeout_s"):
        load_ask_park_timeout_s(stub)
