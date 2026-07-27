"""Phase 5 Task 3 — no TARS write path to a PTY.

P4 deleted the pty_* tools; this pins the guarantee so a future PR that
re-adds terminal-typing can't land silently:

1. The LIVE tool registry (`brain.boot.build_tool_registry` — however
   boot actually assembles it) has no `pty_*`-named tool.
2. No kernel tool source references the write-capable surfaces directly
   (`PtyProcess.write`, `PTYManager._keystroke`) — `_open_for_agent` via
   the `"open"` op is the only sanctioned path, per `phase-5-terminal.md`
   Task 3 and `.superpowers/sdd/task-1-impl-map.md` §8.
3. `PTYManager.dispatch_for_agent` rejects every op except `"open"` —
   companion assertion to the existing parametrized coverage in
   `fix_pass_2026_04_25/test_terminal_operator_pty.py
   ::test_dispatch_for_agent_rejects_unknown_op`, adding the literal
   `write`/`keystroke` verbs the brief names explicitly.
4. The one production call site of `pty_dispatcher`
   (`kernel/tools/start_controller_session.py`) passes the literal
   string `"open"` — not an operator/tool-input-controlled value.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from tesseract.brain.boot import build_tool_registry
from tesseract.mirror.server.config import ShellProfile, TerminalServerConfig
from tesseract.mirror.server.pty_manager import PTYEntry, PTYManager

_KERNEL_TOOLS_DIR = Path(__file__).resolve().parents[3] / "tesseract" / "kernel" / "tools"


def test_no_pty_tool_registered_in_live_registry() -> None:
    registry, _mood, _voice, _bundle, _alarms = build_tool_registry(policy=None)
    names = registry.names()
    pty_named = [n for n in names if n.startswith("pty_") or n.startswith("pty-")]
    assert not pty_named, (
        f"a pty_* tool is registered — TARS would gain a terminal write path: {pty_named}"
    )


def test_no_kernel_tool_source_reaches_write_capable_pty_surfaces() -> None:
    """Static guard: no tool source file references the write-capable
    surfaces directly. Any future tool that did so would bypass
    `dispatch_for_agent`'s op gate entirely."""
    offenders: list[str] = []
    for path in sorted(_KERNEL_TOOLS_DIR.glob("*.py")):
        src = path.read_text(encoding="utf-8")
        if "PtyProcess" in src or "_keystroke" in src:
            offenders.append(path.name)
    assert not offenders, (
        f"kernel tool(s) reference write-capable PTY surfaces directly: {offenders}"
    )


def test_start_controller_session_calls_pty_dispatcher_with_literal_open() -> None:
    """The sole production `pty_dispatcher` call site must pass the
    literal `"open"` string, not a tool-input-controlled value —
    otherwise operator/agent input could smuggle a different op through."""
    from tesseract.kernel.tools import start_controller_session

    src = inspect.getsource(start_controller_session)
    call_site = src[src.index("context.pty_dispatcher("):]
    first_arg_line = call_site.splitlines()[1].strip()
    assert first_arg_line == '"open",', (
        f"start_controller_session's pty_dispatcher call site no longer "
        f"passes a literal \"open\" as its first arg: {first_arg_line!r}"
    )
    # No other string literal is ever passed as the op in this module.
    assert '"write"' not in src
    assert '"keystroke"' not in src


def _build_pty_manager() -> PTYManager:
    cfg = TerminalServerConfig(
        default_shell="bash",
        max_tabs=1,
        max_panes_per_tab=1,
        shell_profiles={"bash": ShellProfile(argv=("bash",), label="bash")},
        coalesce_flush_ms=8.0,
        coalesce_flush_chars=4096,
        reattach_grace_s=30.0,
        pause_buffer_cap_chars=2_000_000,
    )
    pty = PTYManager(cfg)
    app = web.Application()
    pty.bind_app(app)
    return pty


@pytest.mark.parametrize("op", ["write", "keystroke", "type", "send_keys"])
async def test_dispatch_for_agent_rejects_write_and_keystroke_ops(op: str) -> None:
    pty = _build_pty_manager()
    proc = MagicMock()
    proc.isalive.return_value = True
    proc.write = MagicMock(return_value=None)
    entry = PTYEntry(pane_id="p1", shell="bash", proc=proc, ws=MagicMock(), owner="entity")
    pty._ptys["p1"] = entry

    result = await pty.dispatch_for_agent(op, {"pane_id": "p1", "data": "rm -rf /\n"})

    assert result["ok"] is False
    assert "unknown_op" in result["error"]
    proc.write.assert_not_called()
