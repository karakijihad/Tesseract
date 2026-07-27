"""dispatch_for_agent unknown-op rejection.

P4 prune (2026-07-04): `pty_send_keystrokes` and the operator-handoff
gate it exercised were retired along with the other TARS-drives-PTY
tools. `dispatch_for_agent` now serves only the dual-use ``"open"``
verb (`start_controller_session` / boot-time pane reattach); every
other op — including the ones the deleted tools used — must be
rejected as unknown.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp import web

from tesseract.mirror.server.config import ShellProfile, TerminalServerConfig
from tesseract.mirror.server.pty_manager import PTYEntry, PTYManager


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


def _stub_pty_entry(pty: PTYManager, pane_id: str, owner: str = "user") -> PTYEntry:
    proc = MagicMock()
    proc.isalive.return_value = True
    proc.write = MagicMock(return_value=None)
    entry = PTYEntry(pane_id=pane_id, shell="bash", proc=proc, ws=MagicMock(), owner=owner)
    pty._ptys[pane_id] = entry
    return entry


@pytest.mark.parametrize(
    "op", ["unknown_op", "spawn_pane", "destroy_pane", "send_keystrokes", "close", "list"]
)
async def test_dispatch_for_agent_rejects_unknown_op(op: str):
    pty = _build_pty_manager()
    _stub_pty_entry(pty, "p1", owner="entity")

    result = await pty.dispatch_for_agent(op, {"pane_id": "p1"})
    assert result["ok"] is False
    assert "unknown_op" in result["error"]
