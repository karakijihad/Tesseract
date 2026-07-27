"""In-session ``/command`` parser + dispatch.

Tests sit at the dispatcher boundary — :func:`dispatch` runs against a
fake ``_TuiSession`` so we don't need a real TCP daemon. The fake
exposes the same attribute surface the real class does (renderer,
client, session_id, detach_requested, keep_daemon_on_exit) plus an
``AsyncMock``-style client that records every IPC call.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tesseract.orchestrator.tars_controller.ipc_client import (
    ControllerClientError,
)
from tesseract.orchestrator.tars_controller.renderer import TuiRenderer
from tesseract.orchestrator.tars_controller.slash_commands import (
    dispatch,
    is_slash_command,
    known_commands,
)


# ── fakes ──────────────────────────────────────────────────────────────


class _FakeClient:
    """Minimal stub mirroring the subset of :class:`ControllerClient`
    the slash commands use. Each call records its arguments; canned
    return values let tests assert the renderer output the dispatcher
    produced."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.list_sessions_return: list[dict[str, Any]] = []
        self.new_session_return: dict[str, Any] = {
            "session": {"session_id": "2026-05-24-newnewid"}
        }
        self.delete_session_return: dict[str, Any] = {
            "session_id": "2026-05-24-tobegone"
        }
        self.rename_session_return: dict[str, Any] = {
            "session_id": "2026-05-24-abc12345", "title": ""
        }
        self.reload_return: dict[str, Any] = {
            "target": "all", "reloaded": ["roles: ok"], "failed": [],
            "pending_turns": 0,
        }
        self.raise_with: ControllerClientError | None = None

    def _record(self, name: str, *args: Any, **kw: Any) -> None:
        self.calls.append((name, args, kw))

    async def list_sessions(self) -> list[dict[str, Any]]:
        self._record("list_sessions")
        if self.raise_with is not None:
            raise self.raise_with
        return self.list_sessions_return

    async def new_session(self, **kw: Any) -> dict[str, Any]:
        self._record("new_session", **kw)
        if self.raise_with is not None:
            raise self.raise_with
        return self.new_session_return

    async def delete_session(self, sid: str) -> dict[str, Any]:
        self._record("delete_session", sid)
        if self.raise_with is not None:
            raise self.raise_with
        return self.delete_session_return

    async def rename_session(self, sid: str, title: str) -> dict[str, Any]:
        self._record("rename_session", sid, title)
        if self.raise_with is not None:
            raise self.raise_with
        return {**self.rename_session_return, "title": title}

    async def reload(self, target: str = "all") -> dict[str, Any]:
        self._record("reload", target)
        if self.raise_with is not None:
            raise self.raise_with
        return {**self.reload_return, "target": target}


@dataclass
class _FakeTui:
    """Mirrors :class:`tesseract.scripts.tars_cli._TuiSession` for
    dispatch purposes. Only the fields the slash handlers touch."""

    client: _FakeClient
    renderer: TuiRenderer
    stream: io.StringIO
    session_id: str = "2026-05-24-abc12345"
    detach_requested: bool = False
    keep_daemon_on_exit: bool | None = None
    last_worker_id: str | None = None
    last_tool_use_id: str | None = None

    def text(self) -> str:
        """Combined view of ``console.print`` output (via recorder) and
        direct file writes (``/clear``'s ANSI escape)."""
        return self.renderer.recorded_text() + self.stream.getvalue()


def _make_tui(session_id: str = "2026-05-24-abc12345") -> _FakeTui:
    stream = io.StringIO()
    renderer = TuiRenderer(stream=stream, color=False, record=True)
    return _FakeTui(
        client=_FakeClient(),
        renderer=renderer,
        stream=stream,
        session_id=session_id,
    )


# ── predicate ──────────────────────────────────────────────────────────


def test_is_slash_command_true_for_leading_slash() -> None:
    assert is_slash_command("/help") is True
    assert is_slash_command("/delete 2026-05-24-deadbeef") is True


def test_is_slash_command_false_for_non_slash() -> None:
    assert is_slash_command("hello") is False
    assert is_slash_command("") is False
    assert is_slash_command(":quit") is False  # legacy prefix


def test_is_slash_command_passes_through_paths() -> None:
    # Operators mention filesystem paths in chat; they must reach the
    # daemon as ``user_input``, not get parsed as commands.
    assert is_slash_command("/etc/hosts") is False
    assert is_slash_command("/dev/null") is False
    assert is_slash_command("/usr/bin/python") is False
    assert is_slash_command("/") is False  # bare slash


def test_known_commands_includes_core_set() -> None:
    names = set(known_commands())
    # Sanity: the user-confirmed Core + rename + reload set is wired.
    assert {
        "help", "clear", "sessions", "new", "delete",
        "title", "reload", "detach", "quit", "shutdown",
    }.issubset(names)


# ── local-only commands ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_help_renders_each_command(isolated_home: Path) -> None:
    tui = _make_tui()
    await dispatch(tui, "/help")
    out = tui.text()
    for name in known_commands():
        assert f"/{name}" in out, name
    assert tui.client.calls == []


@pytest.mark.asyncio
async def test_clear_emits_ansi_escape(isolated_home: Path) -> None:
    tui = _make_tui()
    await dispatch(tui, "/clear")
    out = tui.text()
    assert "\x1b[2J" in out
    assert "\x1b[H" in out


@pytest.mark.asyncio
async def test_unknown_command_prints_hint(isolated_home: Path) -> None:
    tui = _make_tui()
    await dispatch(tui, "/nope")
    out = tui.text()
    assert "unknown command" in out
    assert "/help" in out


# ── IPC commands ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sessions_lists_with_current_marker(isolated_home: Path) -> None:
    tui = _make_tui()
    tui.client.list_sessions_return = [
        {"session_id": tui.session_id, "status": "active", "title": "self"},
        {"session_id": "2026-05-24-other", "status": "detached", "title": "x"},
    ]
    await dispatch(tui, "/sessions")
    out = tui.text()
    assert tui.session_id in out
    assert "2026-05-24-other" in out
    assert "● " in out  # current-session marker


@pytest.mark.asyncio
async def test_new_prints_minted_session_id(isolated_home: Path) -> None:
    tui = _make_tui()
    await dispatch(tui, "/new my workspace")
    out = tui.text()
    assert "2026-05-24-newnewid" in out
    call = tui.client.calls[0]
    assert call[0] == "new_session"
    assert call[2]["title"] == "my workspace"


@pytest.mark.asyncio
async def test_delete_refuses_current_session(isolated_home: Path) -> None:
    tui = _make_tui()
    await dispatch(tui, f"/delete {tui.session_id}")
    out = tui.text()
    assert "cannot remove the attached session" in out
    # No IPC went out.
    assert tui.client.calls == []


@pytest.mark.asyncio
async def test_delete_sends_ipc_for_other_session(isolated_home: Path) -> None:
    tui = _make_tui()
    await dispatch(tui, "/delete 2026-05-24-tobegone")
    out = tui.text()
    assert "session deleted" in out
    assert "2026-05-24-tobegone" in out
    assert tui.client.calls[0] == ("delete_session", ("2026-05-24-tobegone",), {})


@pytest.mark.asyncio
async def test_delete_requires_id(isolated_home: Path) -> None:
    tui = _make_tui()
    await dispatch(tui, "/delete")
    out = tui.text()
    assert "requires a session id" in out
    assert tui.client.calls == []


@pytest.mark.asyncio
async def test_delete_surfaces_daemon_error(isolated_home: Path) -> None:
    tui = _make_tui()
    tui.client.raise_with = ControllerClientError(
        "session_attached: detach first"
    )
    await dispatch(tui, "/delete 2026-05-24-attachd1")
    out = tui.text()
    assert "delete:" in out
    assert "session_attached" in out


@pytest.mark.asyncio
async def test_title_renames_current_session(isolated_home: Path) -> None:
    tui = _make_tui()
    await dispatch(tui, "/title auth refactor")
    out = tui.text()
    assert "renamed" in out
    assert "auth refactor" in out
    assert tui.client.calls[0] == (
        "rename_session", (tui.session_id, "auth refactor"), {}
    )


@pytest.mark.asyncio
async def test_title_requires_text(isolated_home: Path) -> None:
    tui = _make_tui()
    await dispatch(tui, "/title")
    out = tui.text()
    assert "requires a new title" in out
    assert tui.client.calls == []


@pytest.mark.asyncio
async def test_reload_dispatches_with_default_target(
    isolated_home: Path,
) -> None:
    tui = _make_tui()
    await dispatch(tui, "/reload")
    assert tui.client.calls[0] == ("reload", ("all",), {})


@pytest.mark.asyncio
async def test_reload_rejects_unknown_target(isolated_home: Path) -> None:
    tui = _make_tui()
    await dispatch(tui, "/reload secrets")
    out = tui.text()
    assert "unknown target" in out
    assert tui.client.calls == []


# ── exit commands ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_quit_sets_detach_requested(isolated_home: Path) -> None:
    tui = _make_tui()
    await dispatch(tui, "/quit")
    assert tui.detach_requested is True
    assert tui.keep_daemon_on_exit is None  # respects CLI default


@pytest.mark.asyncio
async def test_detach_keeps_daemon_alive(isolated_home: Path) -> None:
    tui = _make_tui()
    await dispatch(tui, "/detach")
    assert tui.detach_requested is True
    assert tui.keep_daemon_on_exit is True


@pytest.mark.asyncio
async def test_shutdown_forces_daemon_teardown(isolated_home: Path) -> None:
    tui = _make_tui()
    await dispatch(tui, "/shutdown")
    assert tui.detach_requested is True
    assert tui.keep_daemon_on_exit is False
