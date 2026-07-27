"""Audit C1 regression — `execute_tool` must run path validation on
write-side file tools so null bytes, UNC, tilde expansion, and
workspace-boundary escapes are rejected before the tool runs.

Before 2026-04-29, `execute_tool` only consulted `tool.check_permissions`
and `policy.get_posture`. `validate_path` (12-vector check) existed in
`tesseract/permissions/path_validator.py` but was never called on the
live executor path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from tesseract.brain.tools import ToolRegistry, execute_tool
from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.file_write import FileWriteTool


def _registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(FileWriteTool())
    return r


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(workspace_root=str(tmp_path))


def test_null_byte_rejected(tmp_path: Path) -> None:
    result = asyncio.run(execute_tool(
        registry=_registry(),
        tool_name="file_write",
        tool_input={"file_path": "ok\x00.txt", "content": "x"},
        context=_ctx(tmp_path),
    ))
    assert result.is_error
    assert result.denied_hard
    assert "null byte" in result.deny_reason


def test_tilde_expansion_rejected(tmp_path: Path) -> None:
    result = asyncio.run(execute_tool(
        registry=_registry(),
        tool_name="file_write",
        tool_input={"file_path": "~/secrets.txt", "content": "x"},
        context=_ctx(tmp_path),
    ))
    assert result.is_error
    assert result.denied_hard


def test_path_outside_workspace_rejected(tmp_path: Path) -> None:
    """validate_path's vector 12 — workspace boundary escape."""
    result = asyncio.run(execute_tool(
        registry=_registry(),
        tool_name="file_write",
        tool_input={"file_path": "../escape.txt", "content": "x"},
        context=_ctx(tmp_path),
    ))
    assert result.is_error
    assert result.denied_hard


def test_legitimate_relative_write_passes_validation(tmp_path: Path) -> None:
    """A normal in-workspace relative write should not be rejected by the
    validator. (No policy is wired here so it falls through to the tool's
    own ASK / PASSTHROUGH posture.)

    2026-05-17: `FileWriteInput` now normalizes bare-relative paths to
    `tesseract/<path>` so the `path_overrides` DENY rules match. Test
    uses the canonical form directly.
    """
    result = asyncio.run(execute_tool(
        registry=_registry(),
        tool_name="file_write",
        tool_input={"file_path": "tesseract/ok.txt", "content": "hi"},
        context=_ctx(tmp_path),
    ))
    assert not result.is_error
    assert (tmp_path / "tesseract" / "ok.txt").read_text(encoding="utf-8") == "hi"
