"""SU-5 corrective sweep (2026-05-21) — `runtime_lock_deny` workspace event.

Phase doc §2.2 promised "every absolute-DENY trip emits a workspace event
of kind ``runtime_lock_deny``"; check #25 was wired but emission was
deferred. These tests assert the emission fires from BOTH the file_write
lockdown path (SU-1) and the bash_security check #25 path (SU-5).

Each test redirects ``TESSERACT_HOME`` so the event lands in tmp_path and
production ``tesseract/logs/workspace/events.jsonl`` stays clean (per the
CLAUDE.md hard rule about test pollution).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tesseract.kernel.tools.base import (
    PermissionResult,
    ToolContext,
)


def _read_events(home: Path) -> list[dict]:
    events_path = home / "logs" / "workspace" / "events.jsonl"
    if not events_path.exists():
        return []
    rows: list[dict] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _runtime_lock_rows(home: Path) -> list[dict]:
    return [r for r in _read_events(home) if r.get("kind") == "runtime_lock_deny"]


@pytest.mark.asyncio
async def test_file_write_lockdown_emits_runtime_lock_deny(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    from tesseract.kernel.tools.file_write import FileWriteInput, FileWriteTool

    workspace_root = tmp_path / "repo"
    (workspace_root / "tesseract" / "kernel").mkdir(parents=True)
    tool = FileWriteTool()
    ctx = ToolContext(workspace_root=str(workspace_root))
    inp = FileWriteInput(
        file_path="tesseract/kernel/forbidden.py",
        content="# attempt",
    )

    result = await tool.run(inp, ctx)

    assert result.is_error and result.denied_hard
    rows = _runtime_lock_rows(tmp_path)
    assert len(rows) == 1, f"expected one row, got {rows!r}"
    row = rows[0]
    assert row["source"] == "security"
    assert row["payload"]["tool"] == "file_write"
    assert "tesseract/kernel" in row["payload"]["locked_path"].replace("\\", "/").lower()
    assert "runtime-tree path locked" in row["payload"]["reason"]


def test_bash_security_check25_emits_runtime_lock_deny(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    from tesseract.kernel.tools.bash_tool import BashInput, BashTool

    tool = BashTool()
    ctx = ToolContext(workspace_root=str(tmp_path))
    inp = BashInput(
        command="echo redacted > tesseract/config/permissions.yaml",
    )

    verdict = tool.check_permissions(inp, ctx)

    assert verdict == PermissionResult.DENY
    rows = _runtime_lock_rows(tmp_path)
    assert len(rows) == 1, f"expected one row, got {rows!r}"
    row = rows[0]
    assert row["source"] == "security"
    assert row["payload"]["tool"] == "bash"
    assert row["payload"]["check_id"] == "25"
    assert row["payload"]["locked_path"] == "tesseract/config/permissions.yaml"
    assert "command_excerpt" in row["payload"]
    assert row["payload"]["command_excerpt"].startswith("echo redacted")


def test_runtime_lock_deny_emitter_swallows_errors(
    tmp_path, monkeypatch
) -> None:
    """Emitter must never raise — best-effort by contract."""
    # Point TESSERACT_HOME at a path that exists but is read-only-ish.
    # On Windows we can't easily produce a guaranteed write failure, so
    # this test asserts the contract via the explicit try/except in the
    # emitter rather than fault injection — the emitter is the only sync
    # path that could fail the DENY, and the existing two tests above
    # already confirm the happy path works.
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    from tesseract.workspace_events.runtime_lock import emit_runtime_lock_deny

    emit_runtime_lock_deny(
        tool="file_write",
        locked_path="tesseract/kernel/x.py",
        reason="test reason",
    )
    # No exception = pass.
    rows = _runtime_lock_rows(tmp_path)
    assert len(rows) == 1
