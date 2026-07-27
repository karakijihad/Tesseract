"""memory_save honors `subdir`; updates preserve sub-bucket location.

Regression: `memory_save` ignored organizational sub-buckets like
`reference/people/`, dropping every save into the type root. And any
later update/auto-link/librarian rewrite would relocate a sub-bucketed
file back to the type root, orphaning the original file.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.memory_save import MemorySaveInput, MemorySaveTool
from tesseract.memory.fts_index import FTSIndex
from tesseract.memory.index import MemoryIndex
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType


def _bundle(tmp_path: Path) -> tuple[MemoryStore, MemoryIndex, FTSIndex]:
    store_dir = tmp_path / "memory-store"
    derived_dir = store_dir / "derived"
    derived_dir.mkdir(parents=True, exist_ok=True)
    store = MemoryStore(store_dir=store_dir)
    index = MemoryIndex(store_dir=store_dir)
    fts = FTSIndex(db_path=derived_dir / "fts.db")
    return store, index, fts


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        workspace_root=str(tmp_path),
        session_id="test-session",
    )


def test_memory_save_routes_into_subdir(tmp_path: Path) -> None:
    store, index, fts = _bundle(tmp_path)
    tool = MemorySaveTool(store=store, index=index, embeddings=None, fts_index=fts)

    inp = MemorySaveInput(
        type="reference",
        title="Jane Doe",
        content="Public people-reference card. Biomedical engineer based in Springfield, focused on healthcare and medical devices research.",
        importance=7,
        tags=["people-profile"],
        slug="jane_doe_1",
        confidence=0.9,
        entities=["jane doe"],
        subdir="people",
    )
    result = asyncio.run(tool.run(inp, _ctx(tmp_path)))
    assert not result.is_error, result.output

    mem_id = result.output.split()[2]
    expected = tmp_path / "memory-store" / "reference" / "people" / f"{mem_id}.md"
    assert expected.exists(), f"file should land in reference/people/, got tree: {list((tmp_path/'memory-store'/'reference').rglob('*.md'))}"
    flat = tmp_path / "memory-store" / "reference" / f"{mem_id}.md"
    assert not flat.exists(), "must not also exist at the type root"


def test_memory_save_strips_redundant_type_prefix(tmp_path: Path) -> None:
    store, index, fts = _bundle(tmp_path)
    tool = MemorySaveTool(store=store, index=index, embeddings=None, fts_index=fts)

    inp = MemorySaveInput(
        type="reference",
        title="John Doe",
        content="Public people-reference card. Software engineer based in London, building open-source distributed systems and database engines.",
        importance=6,
        slug="john_doe_1",
        subdir="reference/people",
    )
    result = asyncio.run(tool.run(inp, _ctx(tmp_path)))
    assert not result.is_error, result.output
    mem_id = result.output.split()[2]
    expected = tmp_path / "memory-store" / "reference" / "people" / f"{mem_id}.md"
    assert expected.exists()


def test_memory_save_rejects_path_traversal(tmp_path: Path) -> None:
    store, index, fts = _bundle(tmp_path)
    tool = MemorySaveTool(store=store, index=index, embeddings=None, fts_index=fts)

    inp = MemorySaveInput(
        type="reference",
        title="bad",
        content="Should be blocked because subdir tries to escape the store boundary; this body is long enough to not be filtered.",
        slug="bad_subdir_1",
        subdir="../escape",
    )
    result = asyncio.run(tool.run(inp, _ctx(tmp_path)))
    assert result.is_error
    assert "subdir" in result.output.lower()


def test_store_write_preserves_subbucket_on_update(tmp_path: Path) -> None:
    """Once a memory lives in `reference/people/`, a later update must not
    relocate it to `reference/`. This is the latent bug that affected
    memory_update / auto_linker / librarian / dreaming."""
    store, _index, _fts = _bundle(tmp_path)

    now = datetime.now(timezone.utc)
    fm = MemoryFrontmatter(
        id="mem_subbucket_test",
        type=MemoryType.REFERENCE,
        title="Subbucket round-trip",
        summary="seeded",
        created_at=now,
        updated_at=now,
        importance=5,
    )
    body = "Initial body content long enough to clear the librarian floor — checking subbucket round-trip."
    assert store.write(fm, body, subdir_override="reference/people")

    in_subbucket = tmp_path / "memory-store" / "reference" / "people" / f"{fm.id}.md"
    assert in_subbucket.exists()

    fm_updated = fm.model_copy(update={"updated_at": datetime.now(timezone.utc)})
    new_body = body + " Updated."
    assert store.write(fm_updated, new_body)

    assert in_subbucket.exists(), "update must rewrite at the existing path"
    assert not (tmp_path / "memory-store" / "reference" / f"{fm.id}.md").exists(), (
        "update must not orphan a copy at the type root"
    )

    text = in_subbucket.read_text(encoding="utf-8")
    assert "Updated." in text
