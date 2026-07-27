"""End-to-end integration test — chat → terminal handoff flow.

Exercises the full chain with mocks only:
  1. Refuse→redirect: delegate_claude/codex with mirror/** target_paths
  2. Handoff: start_controller_session(launch_terminal=True) opens viewer PTY
  3. Session list tagging: GET /api/controller/sessions tags operator_facing
  4. Reattach scope: reattach_operator_panes opens only operator-facing panes
  5. Teardown: teardown_all_controller_sessions deletes all sessions

No real daemon / subprocess / pty is started.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _Rec:
    """Minimal stand-in for a session record."""

    def __init__(self, sid: str, origin: str) -> None:
        self.session_id = sid
        self.origin = origin
        self.status = "active"
        self.title = None
        self.last_active_at = "2026-05-25T00:00:00.000Z"
        self.mode = "chat"


def _make_mock_session(sid: str) -> MagicMock:
    s = MagicMock()
    s.session_id = sid
    return s


def _make_fake_dispatch(session_id: str) -> Any:
    async def fake_dispatch(prompt: str, **kwargs: Any):
        from tesseract.orchestrator.tars_controller.dispatcher import DispatchResult

        assert kwargs["wait_for_completion"] is False
        assert kwargs["origin"] == "mirror"
        return DispatchResult(session_id=session_id)

    return fake_dispatch


# ---------------------------------------------------------------------------
# 1. Refuse → redirect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_delegate_claude_refuses_mirror_target() -> None:
    """delegate_claude with mirror/** path is blocked before any subprocess runs
    and returns is_error + reason==requires_terminal_handoff."""
    from tesseract.kernel.tools.base import ToolContext
    from tesseract.kernel.tools.delegate_claude import (
        DelegateClaudeInput,
        DelegateClaudeTool,
    )

    tool = DelegateClaudeTool()
    result = await tool.run(
        DelegateClaudeInput(
            task="update the StepDetailPanel",
            target_paths=["tesseract/mirror/src/views/missions/components/StepDetailPanel.tsx"],
            background=False,
        ),
        ToolContext(),
    )

    assert result.is_error is True
    assert result.metadata is not None
    assert result.metadata["reason"] == "requires_terminal_handoff"
    assert "start_controller_session" in result.output
    assert "launch_terminal" in result.output


@pytest.mark.asyncio
async def test_e2e_delegate_codex_refuses_mirror_target() -> None:
    """delegate_codex with mirror/** path is blocked and redirects to controller."""
    from tesseract.kernel.tools.base import ToolContext
    from tesseract.kernel.tools.delegate_codex import (
        DelegateCodexInput,
        DelegateCodexTool,
    )

    tool = DelegateCodexTool()
    result = await tool.run(
        DelegateCodexInput(
            task="review the Mirror server app",
            target_paths=["tesseract/mirror/server/app.py"],
            background=False,
        ),
        ToolContext(),
    )

    assert result.is_error is True
    assert result.metadata is not None
    assert result.metadata["reason"] == "requires_terminal_handoff"
    assert "start_controller_session" in result.output


# ---------------------------------------------------------------------------
# 2. Handoff: start_controller_session(launch_terminal=True)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_handoff_opens_viewer_pty_and_returns_card(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Full handoff path:
    - dispatch_to_controller is mocked → returns a canned session_id
    - pty_dispatcher receives ("open", {name, command}) with no end_of_turn_mode
    - result carries child_transcript_ref metadata (kind, session_id, ws_path)
    - terminal_launched is True in metadata
    """
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    import tesseract.kernel.tools.start_controller_session as mod

    session_id = "2026-05-25-e2e00001"
    monkeypatch.setattr(mod, "dispatch_to_controller", _make_fake_dispatch(session_id))

    opened: dict[str, Any] = {}

    async def fake_pty(action: str, payload: dict[str, Any]) -> dict[str, Any]:
        opened["action"] = action
        opened["payload"] = payload
        return {"ok": True}

    from tesseract.kernel.tools.base import ToolContext
    from tesseract.kernel.tools.start_controller_session import (
        StartControllerSessionInput,
        StartControllerSessionTool,
    )

    ctx = ToolContext(pty_dispatcher=fake_pty)
    tool = StartControllerSessionTool()
    res = await tool.run(
        StartControllerSessionInput(task="edit mirror chat component", launch_terminal=True),
        ctx,
    )

    # Result must not be an error
    assert not res.is_error

    # child_transcript_ref card
    assert res.metadata is not None
    assert res.metadata["kind"] == "child_transcript_ref"
    assert res.metadata["session_id"] == session_id
    assert res.metadata["ws_path"] == f"/ws/controller/{session_id}"
    assert res.metadata["terminal_launched"] is True

    # PTY dispatcher contract
    assert opened["action"] == "open"
    payload = opened["payload"]
    assert payload["name"] == f"ctrl-{session_id}"
    assert payload["command"] == ["tars", "--session", session_id]
    # Viewer PTY must NOT set end_of_turn_mode (interactive TUI, no detector needed)
    assert "end_of_turn_mode" not in payload or payload.get("end_of_turn_mode") is None


# ---------------------------------------------------------------------------
# 3. Session list tagging: GET /api/controller/sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_session_list_tags_operator_facing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /api/controller/sessions tags mirror+cli as operator_facing,
    autonomy as non-operator-facing."""
    import tesseract.mirror.server.routes.controller_sessions as mod
    from tesseract.mirror.server.routes.controller_sessions import (
        OPERATOR_FACING_ORIGINS,
        controller_sessions_handler,
    )

    monkeypatch.setattr(
        mod,
        "_list_active_sessions",
        lambda: [
            _Rec("s-mirror-01", "mirror"),
            _Rec("s-cli-01", "cli"),
            _Rec("s-auto-01", "autonomy"),
        ],
    )

    app = web.Application()
    app.router.add_get("/api/controller/sessions", controller_sessions_handler)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/controller/sessions")
        assert resp.status == 200
        body = await resp.json()

    by_id = {s["session_id"]: s for s in body["sessions"]}

    # mirror → operator_facing
    assert by_id["s-mirror-01"]["operator_facing"] is True
    # cli → operator_facing
    assert by_id["s-cli-01"]["operator_facing"] is True
    # autonomy → not operator_facing
    assert by_id["s-auto-01"]["operator_facing"] is False

    # Sanity check the constant
    assert "mirror" in OPERATOR_FACING_ORIGINS
    assert "cli" in OPERATOR_FACING_ORIGINS
    assert "autonomy" not in OPERATOR_FACING_ORIGINS

    # All sessions have last_active_at
    for s in body["sessions"]:
        assert s["last_active_at"] is not None


# ---------------------------------------------------------------------------
# 4. Reattach scope: boot reopens only operator-facing panes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_reattach_opens_only_operator_facing_panes() -> None:
    """reattach_operator_panes with mirror + cli + autonomy records opens panes
    only for mirror and cli; autonomy session is skipped."""
    from tesseract.mirror.server.app import reattach_operator_panes

    opened: list[dict[str, Any]] = []

    async def fake_pty(action: str, payload: dict[str, Any]) -> dict[str, Any]:
        opened.append({"action": action, "payload": payload})
        return {"ok": True}

    await reattach_operator_panes(
        list_fn=lambda: [
            _Rec("s-mirror-01", "mirror"),
            _Rec("s-auto-01", "autonomy"),
            _Rec("s-cli-01", "cli"),
        ],
        pty_open_fn=fake_pty,
    )

    opened_ids = [o["payload"]["command"][2] for o in opened]

    # Only the operator-facing sessions get panes
    assert "s-mirror-01" in opened_ids
    assert "s-cli-01" in opened_ids
    assert "s-auto-01" not in opened_ids

    # All panes opened with "open" action and correct command shape
    for o in opened:
        assert o["action"] == "open"
        sid = o["payload"]["command"][2]
        assert o["payload"]["name"] == f"ctrl-{sid}"
        assert o["payload"]["command"] == ["tars", "--session", sid]
        assert "end_of_turn_mode" not in o["payload"]


# ---------------------------------------------------------------------------
# 5. Teardown: deliberate shutdown deletes all sessions
# ---------------------------------------------------------------------------


def test_e2e_teardown_deletes_all_sessions() -> None:
    """teardown_all_controller_sessions deletes every session regardless of origin."""
    from tesseract.orchestrator.tars_controller.shutdown import (
        teardown_all_controller_sessions,
    )

    sessions = [
        _make_mock_session("s-mirror-01"),
        _make_mock_session("s-cli-01"),
        _make_mock_session("s-auto-01"),
    ]
    deleted: list[str] = []

    count = teardown_all_controller_sessions(
        list_fn=lambda: sessions,
        delete_fn=lambda sid: deleted.append(sid) or True,  # type: ignore[func-returns-value]
    )

    assert count == 3
    assert set(deleted) == {"s-mirror-01", "s-cli-01", "s-auto-01"}


# ---------------------------------------------------------------------------
# 6. Full flow: refuse → handoff → list → reattach → teardown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_full_chat_terminal_handoff_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Composite flow test — walks the chain in order.

    Step 1: delegate refuses a mirror/** path.
    Step 2: operator calls start_controller_session(launch_terminal=True).
    Step 3: session list returns the new session tagged operator_facing.
    Step 4: on next boot, reattach opens a pane for the session.
    Step 5: on deliberate shutdown, teardown deletes the session.
    """
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    # --- Step 1: delegate refuses mirror/** ---
    from tesseract.kernel.tools.base import ToolContext
    from tesseract.kernel.tools.delegate_claude import (
        DelegateClaudeInput,
        DelegateClaudeTool,
    )

    guard_result = await DelegateClaudeTool().run(
        DelegateClaudeInput(
            task="fix the missions CSS",
            target_paths=["tesseract/mirror/src/styles/missions.css"],
            background=False,
        ),
        ToolContext(),
    )
    assert guard_result.is_error is True
    assert guard_result.metadata["reason"] == "requires_terminal_handoff"

    # --- Step 2: operator calls start_controller_session ---
    import tesseract.kernel.tools.start_controller_session as scs_mod

    session_id = "2026-05-25-e2e00002"
    monkeypatch.setattr(scs_mod, "dispatch_to_controller", _make_fake_dispatch(session_id))

    pty_calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_pty(action: str, payload: dict[str, Any]) -> dict[str, Any]:
        pty_calls.append((action, payload))
        return {"ok": True}

    from tesseract.kernel.tools.start_controller_session import (
        StartControllerSessionInput,
        StartControllerSessionTool,
    )

    handoff = await StartControllerSessionTool().run(
        StartControllerSessionInput(task="fix the missions CSS", launch_terminal=True),
        ToolContext(pty_dispatcher=fake_pty),
    )
    assert not handoff.is_error
    assert handoff.metadata["session_id"] == session_id
    assert handoff.metadata["terminal_launched"] is True
    assert len(pty_calls) == 1
    assert pty_calls[0][1]["command"] == ["tars", "--session", session_id]

    # --- Step 3: session list tags it operator_facing ---
    import tesseract.mirror.server.routes.controller_sessions as route_mod
    from tesseract.mirror.server.routes.controller_sessions import controller_sessions_handler

    monkeypatch.setattr(
        route_mod,
        "_list_active_sessions",
        lambda: [_Rec(session_id, "mirror")],
    )

    app = web.Application()
    app.router.add_get("/api/controller/sessions", controller_sessions_handler)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/controller/sessions")
        body = await resp.json()
    assert body["sessions"][0]["operator_facing"] is True
    assert body["sessions"][0]["session_id"] == session_id

    # --- Step 4: reattach (simulated next boot) ---
    from tesseract.mirror.server.app import reattach_operator_panes

    reattach_calls: list[dict[str, Any]] = []

    async def boot_pty(action: str, payload: dict[str, Any]) -> dict[str, Any]:
        reattach_calls.append({"action": action, "payload": payload})
        return {"ok": True}

    await reattach_operator_panes(
        list_fn=lambda: [_Rec(session_id, "mirror")],
        pty_open_fn=boot_pty,
    )
    assert len(reattach_calls) == 1
    assert reattach_calls[0]["payload"]["command"] == ["tars", "--session", session_id]

    # --- Step 5: deliberate shutdown teardown ---
    from tesseract.orchestrator.tars_controller.shutdown import (
        teardown_all_controller_sessions,
    )

    torn_down: list[str] = []
    count = teardown_all_controller_sessions(
        list_fn=lambda: [_make_mock_session(session_id)],
        delete_fn=lambda sid: torn_down.append(sid) or True,  # type: ignore[func-returns-value]
    )
    assert count == 1
    assert torn_down == [session_id]
