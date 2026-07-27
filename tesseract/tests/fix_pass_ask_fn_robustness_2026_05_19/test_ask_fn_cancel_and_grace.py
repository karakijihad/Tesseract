"""Operator-asked timeout fixes (2026-05-19).

Three contracts pinned:

1. **Grace window** — a late click landing within `ASK_GRACE_SECONDS` after
   the primary timeout still resolves the ask. Without the grace window the
   operator's "yes" pressed at t=29.9s would silently drop because the
   future is popped at t=30.0s. The fix uses `asyncio.shield(fut)` so the
   future itself survives the primary `wait_for`'s internal cancel.

2. **CancelledError path** — if the turn task is cancelled mid-wait, the
   ask_fn must fire a `tool_denied` envelope with `reason='turn_cancelled'`,
   write a `result='cancelled'` audit row, and re-raise the cancel so the
   surrounding task tree exits as expected. Pre-fix, no envelope was sent
   and the UI modal stayed pinned with no audit trail.

3. **cleanup_session** resolves leftover `pending_asks` futures so they
   can't leak after the WS closes (symmetric with the existing
   `pending_overage_asks` treatment).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.mirror.server import ask_gate as ask_gate_mod
from tesseract.mirror.server.session import (
    ASK_GRACE_SECONDS,
    ServerSession,
    _make_ask_fn,
    cleanup_session,
)


class _NoopInput(BaseModel):
    pass


class _NoopTool(Tool):
    name = "_noop"
    description = "test stub"
    input_schema = _NoopInput
    default_posture = "ask"
    risk_class = "operator_gate"

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        return ToolResult(output="ok")


class _FakeWS:
    """Just enough WebSocket surface for ask_fn's send_json calls."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


class _CapturingEventLog:
    def __init__(self) -> None:
        self.envelopes: list[dict[str, Any]] = []

    def append(self, env: dict[str, Any]) -> None:
        self.envelopes.append(env)


@pytest.fixture
def _short_windows(monkeypatch: pytest.MonkeyPatch) -> tuple[float, float]:
    """Slash the timeouts so tests don't take 30+s.

    `_make_ask_fn` is defined in ask_gate.py — its body resolves these two
    names against ask_gate's globals, not session.py's re-exported copies,
    so the patch target must be ask_gate, not session.
    """
    monkeypatch.setattr(ask_gate_mod, "ASK_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(ask_gate_mod, "ASK_GRACE_SECONDS", 0.2)
    return 0.1, 0.2


@pytest.fixture
def _isolate_audit(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Redirect approval_log writes to a temp dir so the test doesn't
    pollute tesseract/logs/approvals.jsonl (CLAUDE.md hard rule)."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))


@pytest.mark.asyncio
async def test_late_click_inside_grace_window_resolves(_short_windows, _isolate_audit) -> None:
    ws = _FakeWS()
    pending: dict[str, asyncio.Future[bool]] = {}
    log_sink = _CapturingEventLog()
    ask = _make_ask_fn(ws, "sess-late", pending, log_sink)  # type: ignore[arg-type]
    ctx = ToolContext()
    ctx.current_call_id = "call-late"

    async def _resolve_after_primary_timeout() -> None:
        # Land the operator click between the primary timeout (0.1s) and
        # the end of the grace window (0.1s + 0.2s = 0.3s).
        await asyncio.sleep(0.15)
        fut = pending["call-late"]
        if not fut.done():
            fut.set_result(True)

    resolver = asyncio.create_task(_resolve_after_primary_timeout())
    approved = await ask(_NoopTool(), _NoopInput(), ctx)
    await resolver

    assert approved is True, "BUG: grace-window click was dropped — operator's yes treated as no"
    # Exactly one tool_ask + one tool_approved envelope should reach the WS.
    sent_types = [e.get("type") for e in ws.sent]
    assert sent_types == ["tool_ask", "tool_approved"], (
        f"BUG: late-click flow emitted wrong envelopes — {sent_types}"
    )


@pytest.mark.asyncio
async def test_primary_timeout_with_no_click_fires_tool_denied(_short_windows, _isolate_audit) -> None:
    ws = _FakeWS()
    pending: dict[str, asyncio.Future[bool]] = {}
    log_sink = _CapturingEventLog()
    ask = _make_ask_fn(ws, "sess-timeout", pending, log_sink)  # type: ignore[arg-type]
    ctx = ToolContext()
    ctx.current_call_id = "call-timeout"

    approved = await ask(_NoopTool(), _NoopInput(), ctx)

    assert approved is False
    sent_types = [e.get("type") for e in ws.sent]
    assert sent_types == ["tool_ask", "tool_denied"], (
        f"BUG: timeout flow did not fire tool_denied — {sent_types}"
    )
    # The future must be popped from pending after the full grace expires.
    assert "call-timeout" not in pending, (
        "BUG: timeout left the future in pending_asks — late tool_response would orphan it"
    )


@pytest.mark.asyncio
async def test_cancelled_turn_fires_tool_denied_with_reason(_short_windows, _isolate_audit) -> None:
    ws = _FakeWS()
    pending: dict[str, asyncio.Future[bool]] = {}
    log_sink = _CapturingEventLog()
    ask = _make_ask_fn(ws, "sess-cancel", pending, log_sink)  # type: ignore[arg-type]
    ctx = ToolContext()
    ctx.current_call_id = "call-cancel"

    ask_task = asyncio.create_task(ask(_NoopTool(), _NoopInput(), ctx))
    # Give ask_fn a moment to register the future before cancelling.
    await asyncio.sleep(0.01)
    ask_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await ask_task

    sent_types = [e.get("type") for e in ws.sent]
    assert "tool_denied" in sent_types, (
        f"BUG: cancellation did not fire tool_denied — UI modal would stay pinned. envelopes={sent_types}"
    )
    denied = next(e for e in ws.sent if e.get("type") == "tool_denied")
    assert denied["data"].get("reason") == "turn_cancelled", (
        f"BUG: tool_denied on cancel missing reason='turn_cancelled' — frontend can't distinguish "
        f"operator-no from turn-cancel. payload={denied}"
    )
    # The future must be cleared from pending so cleanup_session doesn't
    # re-touch it (and so a late _resolve_ask sees no orphan).
    assert "call-cancel" not in pending


@pytest.mark.asyncio
async def test_cleanup_session_resolves_pending_asks(_isolate_audit, tmp_path) -> None:
    """cleanup_session must drain pending_asks (symmetric with overage)."""
    from datetime import datetime, timezone
    from unittest.mock import MagicMock

    # Build a minimal ServerSession dataclass instance. Fields not exercised
    # by cleanup_session can be MagicMocks.
    fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
    session = ServerSession(
        session_id="sess-cleanup",
        ws=_FakeWS(),  # type: ignore[arg-type]
        chat_session=MagicMock(),
        event_log=MagicMock(),
        pending_asks={"call-cleanup": fut},
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    session.current_turn_task = None
    session.tts_synth_task = None

    app = MagicMock()
    app.__getitem__.side_effect = lambda key: {} if key in ("server_sessions", "event_logs") else MagicMock()
    cleanup_session(app, session)

    assert fut.done() and fut.result() is False, (
        "BUG: cleanup_session left a pending_asks future unresolved — future leaks after WS close"
    )
    assert session.pending_asks == {}, (
        "BUG: cleanup_session did not clear pending_asks dict"
    )
