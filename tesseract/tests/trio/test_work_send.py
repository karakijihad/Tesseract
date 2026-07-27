"""W3 — `work_send` unified steer verb: routing matrix over the three
steerable substrates + the one-shot rejection path."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tesseract.brain.spawns import SpawnRegistry
from tesseract.kernel.tools.base import ToolContext, ToolResult
from tesseract.kernel.tools.work_send import WorkSendInput, WorkSendTool


async def _never_done() -> ToolResult:
    await asyncio.Event().wait()
    return ToolResult(output="unreachable")


def _ctx(**kwargs) -> ToolContext:
    return ToolContext(workspace_root=".", session_id="trio-w3-test", **kwargs)


@pytest.mark.asyncio
async def test_one_shot_spawn_handle_rejected():
    registry = SpawnRegistry()
    handle = registry.register(kind="delegate_codex", coro=_never_done())
    ctx = _ctx()
    ctx.spawns = registry

    result = await WorkSendTool().run(
        WorkSendInput(target=handle.handle_id, message="change course"), ctx
    )
    assert result.is_error
    assert result.metadata["reason"] == "unsteerable_one_shot"
    assert "spawn_cancel" in result.output
    await registry.cancel_all()


@pytest.mark.asyncio
async def test_routes_to_interactive_session(monkeypatch):
    from tesseract.kernel.tools import session_tools

    seen = {}

    async def _fake_run(self, tool_input, context):
        seen["handle"] = tool_input.handle
        seen["message"] = tool_input.message
        seen["background"] = tool_input.background
        return ToolResult(output="routed:session")

    monkeypatch.setattr(session_tools.SessionSendTool, "run", _fake_run)

    class _Reg:
        def get(self, handle):
            return object() if handle == "sess-1" else None

    ctx = _ctx()
    ctx.interactive_sessions = _Reg()
    result = await WorkSendTool().run(
        WorkSendInput(target="sess-1", message="steer it"), ctx
    )
    assert result.output == "routed:session"
    assert seen == {"handle": "sess-1", "message": "steer it", "background": True}


@pytest.mark.asyncio
async def test_busy_interactive_session_cancelled_before_resend(monkeypatch):
    """M2: steering a busy interactive session cancels its in-flight turn
    (spawn cancel → kills the CLI subprocess) before the correction runs, and
    clears _pending_spawn_id so the resend isn't rejected as already-in-flight."""
    from tesseract.kernel.tools import session_tools

    seen = {}

    async def _fake_run(self, tool_input, context):
        seen["message"] = tool_input.message
        seen["pending_at_send"] = getattr(sess, "_pending_spawn_id", None)
        return ToolResult(output="routed:session", metadata={})

    monkeypatch.setattr(session_tools.SessionSendTool, "run", _fake_run)

    cancelled = {"ids": []}

    class _Spawns:
        def get(self, x):
            return None  # not a one-shot spawn handle

        async def cancel(self, spawn_id):
            cancelled["ids"].append(spawn_id)
            return True

    class _Sess:
        handle = "sess-1"
        target = "claude"
        _pending_spawn_id = "spawn-abc"

    sess = _Sess()

    class _Reg:
        def get(self, h):
            return sess if h == "sess-1" else None

    ctx = _ctx()
    ctx.interactive_sessions = _Reg()
    ctx.spawns = _Spawns()

    result = await WorkSendTool().run(
        WorkSendInput(target="sess-1", message="steer"), ctx
    )
    assert not result.is_error
    assert cancelled["ids"] == ["spawn-abc"]  # in-flight turn cancelled
    assert sess._pending_spawn_id is None  # cleared before resend
    assert seen["pending_at_send"] is None  # resend saw a free slot
    assert result.metadata["interrupted_prior_turn"] is True


@pytest.mark.asyncio
async def test_routes_to_named_lane(monkeypatch):
    from tesseract.kernel.tools import lane_turn as lane_turn_mod

    seen = {}

    async def _fake_run(self, tool_input, context):
        seen["name_or_id"] = tool_input.name_or_id
        seen["message"] = tool_input.message
        seen["background"] = tool_input.background
        return ToolResult(output="routed:lane")

    monkeypatch.setattr(lane_turn_mod.LaneTurnTool, "run", _fake_run)

    record = SimpleNamespace(lane_id="lane-codex-abc123")
    ctx = _ctx(
        named_lane_manager_provider=lambda: SimpleNamespace(
            get=lambda name: record if name == "auditor/codex" else None
        )
    )
    result = await WorkSendTool().run(
        WorkSendInput(target="auditor/codex", message="re-review please"), ctx
    )
    assert result.output == "routed:lane"
    assert seen["name_or_id"] == "auditor/codex"
    assert seen["background"] is True


@pytest.mark.asyncio
async def test_routes_raw_lane_id_without_named_manager(monkeypatch):
    from tesseract.kernel.tools import lane_turn as lane_turn_mod

    async def _fake_run(self, tool_input, context):
        return ToolResult(output=f"routed:lane:{tool_input.name_or_id}")

    monkeypatch.setattr(lane_turn_mod.LaneTurnTool, "run", _fake_run)

    result = await WorkSendTool().run(
        WorkSendInput(target="lane-claude-7396e5ce4194", message="go"), _ctx()
    )
    assert result.output == "routed:lane:lane-claude-7396e5ce4194"


@pytest.mark.asyncio
async def test_routes_to_controller_session(monkeypatch):
    sent = {}

    class _FakeClient:
        async def user_input(self, session_id, text, await_ack=False):
            sent["session_id"] = session_id
            sent["text"] = text
            sent["await_ack"] = await_ack

        async def close(self):
            sent["closed"] = True

    async def _fake_connect(**kwargs):
        return _FakeClient()

    from tesseract.orchestrator.tars_controller import ipc_client

    monkeypatch.setattr(
        ipc_client.ControllerClient, "connect", classmethod(
            lambda cls, **kw: _fake_connect()
        )
    )

    result = await WorkSendTool().run(
        WorkSendInput(target="2026-07-10-deadbeef", message="new priority"), _ctx()
    )
    assert not result.is_error
    assert sent == {
        "session_id": "2026-07-10-deadbeef",
        "text": "new priority",
        "await_ack": True,  # M9 — steer must confirm delivery, not fire-and-forget
        "closed": True,
    }
    assert result.metadata["route"] == "controller_user_input"


@pytest.mark.asyncio
async def test_controller_steer_reports_error_on_delivery_failure(monkeypatch):
    # M9: a stale session id whose ack comes back session_not_found must surface
    # as an error, not a false success.
    from tesseract.orchestrator.tars_controller.ipc_client import (
        ControllerClientError,
    )

    class _FailClient:
        async def user_input(self, session_id, text, await_ack=False):
            raise ControllerClientError("session_not_found: 2026-07-10-deadbeef")

        async def close(self):
            pass

    from tesseract.orchestrator.tars_controller import ipc_client

    monkeypatch.setattr(
        ipc_client.ControllerClient,
        "connect",
        classmethod(lambda cls, **kw: _fail_connect()),
    )

    async def _fail_connect():
        return _FailClient()

    result = await WorkSendTool().run(
        WorkSendInput(target="2026-07-10-deadbeef", message="new priority"), _ctx()
    )
    assert result.is_error
    assert result.metadata["reason"] == "user_input_failed"


@pytest.mark.asyncio
async def test_busy_lane_interrupted_before_resend(monkeypatch):
    """M2: steering a busy lane cancels its in-flight turn (interrupt) before
    the correction runs — cancel + resend, not queue-behind-lock. ('not
    attached' self-heal now lives in lane_turn, tested there.)"""
    from tesseract.kernel.tools import lane_turn as lane_turn_mod

    calls = {"interrupted": [], "turns": 0}

    async def _fake_run(self, tool_input, context):
        calls["turns"] += 1
        return ToolResult(
            output="routed:lane", metadata={"lane_id": tool_input.name_or_id}
        )

    monkeypatch.setattr(lane_turn_mod.LaneTurnTool, "run", _fake_run)

    class _LaneMgr:
        async def interrupt(self, lane_id):
            calls["interrupted"].append(lane_id)
            return True

    record = SimpleNamespace(lane_id="lane-codex-abc123")
    ctx = _ctx(
        named_lane_manager_provider=lambda: SimpleNamespace(
            get=lambda name: record if name == "auditor/codex" else None
        ),
        lane_manager_provider=_LaneMgr,
    )
    result = await WorkSendTool().run(
        WorkSendInput(target="auditor/codex", message="new priority"), ctx
    )
    assert not result.is_error
    assert calls["interrupted"] == ["lane-codex-abc123"]  # cancelled first
    assert calls["turns"] == 1  # then the correction runs
    assert result.metadata["interrupted_prior_turn"] is True


@pytest.mark.asyncio
async def test_idle_lane_not_interrupted(monkeypatch):
    """interrupt() returns False on an idle lane — no 'interrupted' marker."""
    from tesseract.kernel.tools import lane_turn as lane_turn_mod

    async def _fake_run(self, tool_input, context):
        return ToolResult(output="routed:lane", metadata={})

    monkeypatch.setattr(lane_turn_mod.LaneTurnTool, "run", _fake_run)

    class _LaneMgr:
        async def interrupt(self, lane_id):
            return False  # idle

    result = await WorkSendTool().run(
        WorkSendInput(target="lane-claude-idle01", message="go"),
        _ctx(lane_manager_provider=_LaneMgr),
    )
    assert not result.is_error
    assert "interrupted_prior_turn" not in (result.metadata or {})


@pytest.mark.asyncio
async def test_unknown_target_errors_with_guidance():
    result = await WorkSendTool().run(
        WorkSendInput(target="nonsense-target", message="hello"), _ctx()
    )
    assert result.is_error
    assert result.metadata["reason"] == "unknown_target"
    assert "named lane" in result.output
