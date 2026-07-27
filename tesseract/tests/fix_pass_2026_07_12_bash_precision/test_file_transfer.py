"""FileCopyTool / FileMoveTool — happy paths, lockdown, overwrite semantics.

TESSERACT_HOME is monkeypatched per-test (hard rule): the lockdown-deny
path emits a runtime_lock event and workshop-destination transfers index
into work_index.sqlite — both resolve their target dir at call time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.file_transfer import (
    FileCopyTool,
    FileMoveTool,
    FileTransferInput,
)
from tesseract.permissions.decide import _WRITE_PATH_TOOLS


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path / "tesseract"))


@pytest.fixture()
def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        workspace_root=str(tmp_path),
        session_id="test-file-transfer-2026-07-12",
        current_call_id="call-file-transfer-01",
    )


def _seed(tmp_path: Path, rel: str, content: str = "payload") -> Path:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


@pytest.mark.asyncio
async def test_copy_happy_path(tmp_path: Path, ctx: ToolContext) -> None:
    _seed(tmp_path, "tesseract/workspace/a.html", "<b>hi</b>")
    result = await FileCopyTool().run(
        FileTransferInput(
            source_path="workspace/a.html",
            dest_path="tars-workshop/2026-07-12/a.html",
        ),
        ctx,
    )
    assert result.is_error is not True
    copied = tmp_path / "tesseract/tars-workshop/2026-07-12/a.html"
    assert copied.read_text(encoding="utf-8") == "<b>hi</b>"
    # source untouched
    assert (tmp_path / "tesseract/workspace/a.html").exists()


@pytest.mark.asyncio
async def test_move_happy_path(tmp_path: Path, ctx: ToolContext) -> None:
    _seed(tmp_path, "tesseract/workspace/b.txt", "move me")
    result = await FileMoveTool().run(
        FileTransferInput(
            source_path="workspace/b.txt",
            dest_path="tars-workshop/2026-07-12/b.txt",
        ),
        ctx,
    )
    assert result.is_error is not True
    assert not (tmp_path / "tesseract/workspace/b.txt").exists()
    moved = tmp_path / "tesseract/tars-workshop/2026-07-12/b.txt"
    assert moved.read_text(encoding="utf-8") == "move me"


@pytest.mark.asyncio
async def test_copy_refuses_overwrite_by_default(tmp_path: Path, ctx: ToolContext) -> None:
    _seed(tmp_path, "tesseract/workspace/c.txt", "new")
    _seed(tmp_path, "tesseract/tars-workshop/c.txt", "old")
    inp = FileTransferInput(source_path="workspace/c.txt", dest_path="tars-workshop/c.txt")
    result = await FileCopyTool().run(inp, ctx)
    assert result.is_error is True
    assert "overwrite" in result.output
    assert (tmp_path / "tesseract/tars-workshop/c.txt").read_text(encoding="utf-8") == "old"

    inp = FileTransferInput(
        source_path="workspace/c.txt", dest_path="tars-workshop/c.txt", overwrite=True
    )
    result = await FileCopyTool().run(inp, ctx)
    assert result.is_error is not True
    assert (tmp_path / "tesseract/tars-workshop/c.txt").read_text(encoding="utf-8") == "new"


@pytest.mark.asyncio
async def test_copy_dest_under_kernel_tree_denied(tmp_path: Path, ctx: ToolContext) -> None:
    _seed(tmp_path, "tesseract/workspace/tool.py", "print('x')")
    result = await FileCopyTool().run(
        FileTransferInput(
            source_path="workspace/tool.py",
            dest_path="kernel/tools/evil.py",
        ),
        ctx,
    )
    assert result.is_error is True
    assert result.denied_hard is True
    assert not (tmp_path / "tesseract/kernel/tools/evil.py").exists()


@pytest.mark.asyncio
async def test_move_source_under_kernel_tree_denied(tmp_path: Path, ctx: ToolContext) -> None:
    _seed(tmp_path, "tesseract/kernel/tools/real_tool.py", "print('x')")
    result = await FileMoveTool().run(
        FileTransferInput(
            source_path="kernel/tools/real_tool.py",
            dest_path="workspace/stolen.py",
        ),
        ctx,
    )
    assert result.is_error is True
    assert result.denied_hard is True
    assert (tmp_path / "tesseract/kernel/tools/real_tool.py").exists()


@pytest.mark.asyncio
async def test_copy_source_from_kernel_tree_allowed(tmp_path: Path, ctx: ToolContext) -> None:
    """Copy source is a read — same posture as file_read."""
    _seed(tmp_path, "tesseract/kernel/tools/real_tool.py", "print('x')")
    result = await FileCopyTool().run(
        FileTransferInput(
            source_path="kernel/tools/real_tool.py",
            dest_path="workspace/copy_of_tool.py",
        ),
        ctx,
    )
    assert result.is_error is not True
    assert (tmp_path / "tesseract/workspace/copy_of_tool.py").exists()


@pytest.mark.asyncio
async def test_missing_source_and_dir_source_are_clean_errors(
    tmp_path: Path, ctx: ToolContext
) -> None:
    result = await FileCopyTool().run(
        FileTransferInput(source_path="workspace/nope.txt", dest_path="workspace/out.txt"),
        ctx,
    )
    assert result.is_error is True
    assert "source not found" in result.output

    (tmp_path / "tesseract/workspace/adir").mkdir(parents=True)
    result = await FileCopyTool().run(
        FileTransferInput(source_path="workspace/adir", dest_path="workspace/out.txt"),
        ctx,
    )
    assert result.is_error is True
    assert "directory" in result.output


def test_bare_relative_paths_normalize_under_tesseract() -> None:
    inp = FileTransferInput(source_path="workspace/a.txt", dest_path="tars-workshop/b.txt")
    assert inp.source_path == "tesseract/workspace/a.txt"
    assert inp.dest_path == "tesseract/tars-workshop/b.txt"


def test_write_path_tools_wiring() -> None:
    """decide.evaluate validates dest for copy, both ends for move."""
    assert _WRITE_PATH_TOOLS["file_copy"] == ("dest_path",)
    assert _WRITE_PATH_TOOLS["file_move"] == ("source_path", "dest_path")
    assert "file_path" in _WRITE_PATH_TOOLS["file_write"]


def test_default_postures_declared() -> None:
    assert FileCopyTool.default_posture == "ask"
    assert FileMoveTool.default_posture == "ask"
