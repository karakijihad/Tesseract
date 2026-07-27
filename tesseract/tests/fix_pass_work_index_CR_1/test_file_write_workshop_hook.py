"""m2 follow-up to CR-1: file_write under tars-workshop/ post-hook
indexes the artifact into the work-history index.

End-to-end: write file via the tool's `run()` path → query
WorkIndex → assert the new chunk is retrievable. Best-effort:
hook failures must not bubble up as tool errors.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.file_write import FileWriteInput, FileWriteTool
from tesseract.memory.work_index import WorkIndex


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))


@pytest.mark.asyncio
async def test_workshop_write_indexes_into_work_index(tmp_path: Path) -> None:
    tool = FileWriteTool()
    rel_path = "tars-workshop/2026-05-22/test-artifact/README.md"
    body = (
        "# Test artifact\n\nA hand-written workshop note that the "
        "file_write hook should index automatically.\n"
    )
    result = await tool.run(
        FileWriteInput(file_path=rel_path, content=body),
        ToolContext(workspace_root=str(tmp_path)),
    )
    assert not result.is_error, result.output
    # Indexed under TESSERACT_HOME/work_index.sqlite, same DB the
    # save_session hook + recall_history tool open.
    idx = WorkIndex(tmp_path / "work_index.sqlite")
    hits = idx.search("workshop note", top_k=5)
    assert any(h.source == "workshop" for h in hits), (
        f"workshop write not indexed: {[(h.source, h.source_ref) for h in hits]}"
    )
    assert any(h.source_ref == "test-artifact" for h in hits)


@pytest.mark.asyncio
async def test_non_workshop_write_skips_indexing(tmp_path: Path) -> None:
    """Writes outside tars-workshop/ should not touch the index."""
    tool = FileWriteTool()
    result = await tool.run(
        FileWriteInput(file_path="agents/some-note.md",
                       content="not a workshop file"),
        ToolContext(workspace_root=str(tmp_path)),
    )
    assert not result.is_error
    idx = WorkIndex(tmp_path / "work_index.sqlite")
    assert idx.count() == 0


@pytest.mark.asyncio
async def test_non_markdown_workshop_write_skips_indexing(tmp_path: Path) -> None:
    """The hook only indexes .md / .txt. Binary or other suffixes
    survive — they are not text artifacts."""
    tool = FileWriteTool()
    result = await tool.run(
        FileWriteInput(file_path="tars-workshop/2026-05-22/asset/data.json",
                       content='{"hello": "world"}'),
        ToolContext(workspace_root=str(tmp_path)),
    )
    assert not result.is_error
    idx = WorkIndex(tmp_path / "work_index.sqlite")
    assert idx.count() == 0
