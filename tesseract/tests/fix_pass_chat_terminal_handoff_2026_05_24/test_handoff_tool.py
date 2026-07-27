"""start_controller_session(launch_terminal=True) — viewer PTY wiring tests.

Real pty_dispatcher contract (confirmed from pty_manager.py _open_for_agent):
    await context.pty_dispatcher("open", payload)
    payload keys: name (str), command (non-empty list of non-empty str), optional cwd
    Returns: dict with "ok" key.

The viewer pane runs `tars --session <id>` as an interactive TUI; there is no
turn-detector to wire, so end_of_turn_mode must be OMITTED (None).
"""

from __future__ import annotations

from typing import Any

import pytest

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.start_controller_session import (
    StartControllerSessionInput,
    StartControllerSessionTool,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_dispatch(session_id: str) -> Any:
    async def fake_dispatch(prompt: str, **kwargs: Any):
        from tesseract.orchestrator.tars_controller.dispatcher import DispatchResult
        assert kwargs["wait_for_completion"] is False
        assert kwargs["origin"] == "mirror"
        return DispatchResult(session_id=session_id)

    return fake_dispatch


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_launch_terminal_opens_viewer_pty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """launch_terminal=True must call pty_dispatcher("open", ...) with
    the tars --session command after the controller session is minted."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    import tesseract.kernel.tools.start_controller_session as mod

    session_id = "2026-05-24-abcd1234"
    monkeypatch.setattr(mod, "dispatch_to_controller", _make_fake_dispatch(session_id))

    opened: dict[str, Any] = {}

    async def fake_pty(action: str, payload: dict[str, Any]) -> dict[str, Any]:
        opened["action"] = action
        opened["payload"] = payload
        return {"ok": True}

    ctx = ToolContext(pty_dispatcher=fake_pty)
    tool = StartControllerSessionTool()
    res = await tool.run(
        StartControllerSessionInput(task="edit mirror chat", launch_terminal=True),
        ctx,
    )

    assert not res.is_error
    assert res.metadata is not None
    assert res.metadata["session_id"] == session_id
    assert opened["action"] == "open"
    payload = opened["payload"]
    # command must be exactly ["tars", "--session", <session_id>]
    assert payload["command"] == ["tars", "--session", session_id]
    assert isinstance(payload["command"], list) and all(payload["command"])
    # viewer pane is a plain interactive TUI — no turn-detector to wire
    # (P4 prune, 2026-07-04: `_open_for_agent` no longer accepts or
    # validates `end_of_turn_mode` at all — the detector substrate was
    # retired with the TARS-drives-PTY tools). end_of_turn_mode must be
    # absent or None.
    mode = payload.get("end_of_turn_mode")
    assert mode is None, f"end_of_turn_mode={mode!r}; viewer PTY must omit it (None)"
    # terminal_launched flag in metadata
    assert res.metadata.get("terminal_launched") is True


@pytest.mark.asyncio
async def test_launch_terminal_false_does_not_open_pty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """launch_terminal=False (default) must NOT call pty_dispatcher."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    import tesseract.kernel.tools.start_controller_session as mod

    session_id = "2026-05-24-ef567890"
    monkeypatch.setattr(mod, "dispatch_to_controller", _make_fake_dispatch(session_id))

    called = {"pty": False}

    async def fake_pty(action: str, payload: dict[str, Any]) -> dict[str, Any]:
        called["pty"] = True
        return {"ok": True}

    ctx = ToolContext(pty_dispatcher=fake_pty)
    tool = StartControllerSessionTool()
    res = await tool.run(
        StartControllerSessionInput(task="x", launch_terminal=False),
        ctx,
    )

    assert not res.is_error
    assert called["pty"] is False
    assert res.metadata is not None
    assert "terminal_launched" not in res.metadata


@pytest.mark.asyncio
async def test_launch_terminal_no_dispatcher_skips_gracefully(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """When pty_dispatcher is None (REPL context), launch_terminal=True
    must not raise — handoff card still returns without terminal_launched."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    import tesseract.kernel.tools.start_controller_session as mod

    session_id = "2026-05-24-cafe0011"
    monkeypatch.setattr(mod, "dispatch_to_controller", _make_fake_dispatch(session_id))

    ctx = ToolContext(pty_dispatcher=None)
    tool = StartControllerSessionTool()
    res = await tool.run(
        StartControllerSessionInput(task="watch this", launch_terminal=True),
        ctx,
    )

    assert not res.is_error
    assert res.metadata is not None
    assert res.metadata["session_id"] == session_id
    # No dispatcher — terminal_launched key must be absent
    assert "terminal_launched" not in res.metadata


@pytest.mark.asyncio
async def test_launch_terminal_pty_exception_does_not_abort_handoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """If the pty dispatcher raises, the handoff card still returns (no reraise);
    terminal_launched is False in metadata to signal the failure."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    import tesseract.kernel.tools.start_controller_session as mod

    session_id = "2026-05-24-deadbeef"
    monkeypatch.setattr(mod, "dispatch_to_controller", _make_fake_dispatch(session_id))

    async def boom_pty(action: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("no_primary_ws")

    ctx = ToolContext(pty_dispatcher=boom_pty)
    tool = StartControllerSessionTool()
    res = await tool.run(
        StartControllerSessionInput(task="risky", launch_terminal=True),
        ctx,
    )

    assert not res.is_error
    assert res.metadata is not None
    assert res.metadata["session_id"] == session_id
    assert res.metadata.get("terminal_launched") is False
