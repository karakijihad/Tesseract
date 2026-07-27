"""SU-1 — FileWriteTool runtime-tree lockdown tests.

Verifies that the hardcoded DENY check in FileWriteTool.run() blocks writes
to the live runtime tree, config files, and path-traversal/symlink escapes,
while allowing writes to workspace/, agents/, tars-workshop/, and unlocked
config files.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic import ValidationError

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.file_write import (
    FileWriteTool,
    FileWriteInput,
    _check_runtime_lockdown,
    _resolve_for_check,
)

_DENY_SUFFIX = (
    "TARS cannot edit the live runtime. "
    "Delegate new tools to Claude/Codex for operator review and promotion, "
    "or write to workspace/ / agents/ / tars-workshop/."
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tool() -> FileWriteTool:
    return FileWriteTool()


@pytest.fixture()
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ToolContext:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return ToolContext(workspace_root=str(tmp_path))


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _run(tool: FileWriteTool, ctx: ToolContext, rel_path: str, content: str = "x"):
    return asyncio.run(
        tool.run(FileWriteInput(file_path=rel_path, content=content), ctx)
    )


# ---------------------------------------------------------------------------
# 1. Locked-path denies (10 tests)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rel_path,expected_reason_substr", [
    (
        "tesseract/kernel/tools/foo.py",
        "runtime-tree path locked: tesseract/kernel/tools/foo.py",
    ),
    (
        "tesseract/orchestrator/autonomy/mappers/operator_view.py",
        "runtime-tree path locked: tesseract/orchestrator/autonomy/mappers/operator_view.py",
    ),
    (
        "tesseract/brain/prompt.py",
        "runtime-tree path locked: tesseract/brain/prompt.py",
    ),
    (
        "tesseract/scheduler/tasks/daily_brief.py",
        "runtime-tree path locked: tesseract/scheduler/tasks/daily_brief.py",
    ),
    (
        "tesseract/mirror/server/app.py",
        "runtime-tree path locked: tesseract/mirror/server/app.py",
    ),
    (
        "tesseract/supervisor/daemon.py",
        "runtime-tree path locked: tesseract/supervisor/daemon.py",
    ),
    (
        "tesseract/config/permissions.yaml",
        "runtime config locked: tesseract/config/permissions.yaml",
    ),
    (
        "tesseract/config/roles.yaml",
        "runtime config locked: tesseract/config/roles.yaml",
    ),
    (
        "tesseract/config/providers.yaml",
        "runtime config locked: tesseract/config/providers.yaml",
    ),
    (
        "tesseract/config/mirror.yaml",
        "runtime config locked: tesseract/config/mirror.yaml",
    ),
])
def test_locked_path_deny(
    tool: FileWriteTool,
    ctx: ToolContext,
    rel_path: str,
    expected_reason_substr: str,
) -> None:
    result = _run(tool, ctx, rel_path)
    assert result.is_error is True
    assert result.denied_hard is True
    assert expected_reason_substr in result.deny_reason
    assert _DENY_SUFFIX in result.deny_reason
    # File must NOT have been created under the tmp workspace
    assert not (Path(ctx.workspace_root) / rel_path).exists()


# ---------------------------------------------------------------------------
# 2. Allow-path passes (11 tests)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rel_path", [
    "tesseract/workspace/SOUL.md",
    "tesseract/workspace/notes/2026-05-20.md",
    "tesseract/agents/pending/new-helper.md",
    "tesseract/agents/live-agent.md",
    "tesseract/tars-workshop/2026-05-20/scratch.py",
    "tesseract/provisional/new-tool/source.py",
    "tesseract/vault/inbox/new-paper.md",
    "tesseract/memory-store/leaves/active/x.md",
    "tesseract/config/schedule.yaml",
    "tesseract/config/agenda.yaml",
    "tesseract/config/identity.yaml",
])
def test_allow_path_passes(
    tool: FileWriteTool,
    ctx: ToolContext,
    rel_path: str,
) -> None:
    content = "test-content"
    result = _run(tool, ctx, rel_path, content)
    assert result.is_error is False
    assert result.denied_hard is False
    written = Path(ctx.workspace_root) / rel_path
    assert written.exists()
    assert written.read_text(encoding="utf-8") == content


# ---------------------------------------------------------------------------
# 3. Escape attempts — must DENY (4 tests)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rel_path,expected_reason_substr", [
    (
        "tesseract/workspace/../kernel/tools/x.py",
        "runtime-tree path locked: tesseract/kernel/tools/x.py",
    ),
    (
        "tesseract/workspace/../orchestrator/autonomy/strategist.py",
        "runtime-tree path locked: tesseract/orchestrator/autonomy/strategist.py",
    ),
    (
        "tesseract/tars-workshop/../config/permissions.yaml",
        "runtime config locked: tesseract/config/permissions.yaml",
    ),
])
def test_dotdot_escape_deny(
    tool: FileWriteTool,
    ctx: ToolContext,
    rel_path: str,
    expected_reason_substr: str,
) -> None:
    result = _run(tool, ctx, rel_path)
    assert result.is_error is True
    assert result.denied_hard is True
    assert expected_reason_substr in result.deny_reason
    assert _DENY_SUFFIX in result.deny_reason


def test_absolute_path_kernel_deny(
    tool: FileWriteTool,
    ctx: ToolContext,
) -> None:
    abs_path = str(Path(ctx.workspace_root) / "tesseract" / "kernel" / "tools" / "x.py")
    result = asyncio.run(
        tool.run(FileWriteInput(file_path=abs_path, content="x"), ctx)
    )
    assert result.is_error is True
    assert result.denied_hard is True
    assert "runtime-tree path locked: tesseract/kernel/tools/x.py" in result.deny_reason
    assert _DENY_SUFFIX in result.deny_reason
    assert not Path(abs_path).exists()


# ---------------------------------------------------------------------------
# 4. Symlink escape (Windows-skip if no symlink permission)
# ---------------------------------------------------------------------------

def test_symlink_escape_deny(
    tool: FileWriteTool,
    ctx: ToolContext,
    tmp_path: Path,
) -> None:
    workspace_tesseract = Path(ctx.workspace_root) / "tesseract"
    workspace_tesseract.mkdir(parents=True, exist_ok=True)
    workspace_dir = workspace_tesseract / "workspace"
    workspace_dir.mkdir(exist_ok=True)
    kernel_target = Path(ctx.workspace_root) / "tesseract" / "kernel" / "tools"
    kernel_target.mkdir(parents=True, exist_ok=True)
    symlink_path = workspace_dir / "sneaky"
    try:
        symlink_path.symlink_to(kernel_target)
    except (OSError, PermissionError):
        pytest.skip("symlink not available")

    result = _run(tool, ctx, "tesseract/workspace/sneaky/x.py")
    assert result.is_error is True
    assert result.denied_hard is True
    assert "runtime-tree path locked:" in result.deny_reason
    assert _DENY_SUFFIX in result.deny_reason


# ---------------------------------------------------------------------------
# 5. Edge cases (3 tests)
# ---------------------------------------------------------------------------

def test_mixed_separators_deny(
    tool: FileWriteTool,
    ctx: ToolContext,
) -> None:
    # Windows mixed separators — backslash + forward slash
    result = _run(tool, ctx, "tesseract\\kernel/tools/x.py")
    assert result.is_error is True
    assert result.denied_hard is True
    assert "runtime-tree path locked:" in result.deny_reason
    assert _DENY_SUFFIX in result.deny_reason


def test_empty_path_returns_error(
    tool: FileWriteTool,
    ctx: ToolContext,
) -> None:
    # Empty path is allowed by _normalize_under_tesseract (returns early) and
    # resolves to the workspace root itself. Writing to a directory path is an
    # OSError — the tool must return is_error=True, not raise.
    result = _run(tool, ctx, "")
    assert result.is_error is True


def test_posture_override_cannot_bypass(
    tool: FileWriteTool,
    ctx: ToolContext,
) -> None:
    # Simulate posture_source="path" (path override marker) — lockdown must
    # still fire because it runs BEFORE posture lookup.
    ctx.posture_source = "path"
    result = _run(tool, ctx, "tesseract/kernel/tools/x.py")
    assert result.is_error is True
    assert result.denied_hard is True
    assert "runtime-tree path locked: tesseract/kernel/tools/x.py" in result.deny_reason
    assert _DENY_SUFFIX in result.deny_reason
    assert not (Path(ctx.workspace_root) / "tesseract" / "kernel" / "tools" / "x.py").exists()


# ---------------------------------------------------------------------------
# 6. Unit tests for helpers
# ---------------------------------------------------------------------------

def test_resolve_for_check_relative(tmp_path: Path) -> None:
    resolved = _resolve_for_check("tesseract/kernel/tools/foo.py", tmp_path)
    expected = (tmp_path / "tesseract" / "kernel" / "tools" / "foo.py").resolve()
    assert resolved == expected


def test_resolve_for_check_absolute(tmp_path: Path) -> None:
    abs_p = str(tmp_path / "tesseract" / "kernel" / "tools" / "foo.py")
    resolved = _resolve_for_check(abs_p, tmp_path)
    assert resolved == Path(abs_p).resolve()


def test_check_runtime_lockdown_kernel(tmp_path: Path) -> None:
    resolved = (tmp_path / "tesseract" / "kernel" / "tools" / "foo.py").resolve()
    reason = _check_runtime_lockdown(resolved, tmp_path)
    assert reason == "runtime-tree path locked: tesseract/kernel/tools/foo.py"


def test_check_runtime_lockdown_config(tmp_path: Path) -> None:
    resolved = (tmp_path / "tesseract" / "config" / "permissions.yaml").resolve()
    reason = _check_runtime_lockdown(resolved, tmp_path)
    assert reason == "runtime config locked: tesseract/config/permissions.yaml"


def test_check_runtime_lockdown_allow(tmp_path: Path) -> None:
    resolved = (tmp_path / "tesseract" / "workspace" / "SOUL.md").resolve()
    reason = _check_runtime_lockdown(resolved, tmp_path)
    assert reason is None


def test_check_runtime_lockdown_outside_workspace(tmp_path: Path) -> None:
    # Path outside workspace — not a lockdown concern (returns None)
    outside = Path("/tmp/some-other-dir/foo.py").resolve()
    reason = _check_runtime_lockdown(outside, tmp_path)
    assert reason is None


# ---------------------------------------------------------------------------
# 7. Case-insensitive lockdown bypass (Windows / macOS case-insensitive FS)
# ---------------------------------------------------------------------------

def test_case_variants_denied(ctx: ToolContext) -> None:
    """Locked prefixes match case-insensitively (Windows + macOS are case-insensitive FS).

    Without lowercasing the comparison, `tesseract/KERNEL/tools/x.py` on Windows would
    resolve to the real (case-folded) location under kernel/ but the lockdown's
    startswith() against lowercase `tesseract/kernel` would miss it.
    """
    tool = FileWriteTool()
    for variant in (
        "tesseract/KERNEL/tools/x.py",
        "tesseract/Orchestrator/autonomy/x.py",
        "tesseract/SCHEDULER/tasks/x.py",
        "tesseract/Config/permissions.yaml",
        "tesseract/CONFIG/ROLES.yaml",
    ):
        result = asyncio.run(
            tool.run(FileWriteInput(file_path=variant, content="x"), ctx)
        )
        assert result.is_error, f"variant should DENY: {variant}"
        assert result.denied_hard, f"variant should DENY hard: {variant}"
        assert "locked" in result.deny_reason
