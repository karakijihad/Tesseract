"""F1 runtime-wiring regression tests (post-audit 2026-04-21).

Covers the paths that were scaffolding-only at the time of audit:
  - Librarian.run_pass() actually promotes sections from daily/*.md
    into canonical subdirs (was: returned 0/0/0).
  - MemoryStore.list_daily_notes() reads from daily/ (was: read memory/).
  - dedupe is wired into MemorySaveTool (was: never called at save time).
  - MemoryBundle exposes a live Librarian (was: not on the bundle).
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from tesseract.memory.librarian import Librarian, _parse_daily_sections
from tesseract.memory.store import MemoryStore


def test_list_daily_notes_reads_daily_dir(tmp_path: Path) -> None:
    store = MemoryStore(store_dir=tmp_path)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    (tmp_path / "daily" / f"{yesterday}.md").write_text(
        "# heading\n\n## Observation\nbody text\n",
        encoding="utf-8",
    )
    notes = store.list_daily_notes()
    assert len(notes) == 1
    assert notes[0].name == f"{yesterday}.md"


def test_parse_daily_sections_splits_on_h2() -> None:
    text = (
        "# 2026-04-20\n\n"
        "pre-heading preamble\n\n"
        "## Observation A\n\n"
        "body A\n\n"
        "## Observation B\n\n"
        "body B\n"
    )
    sections = _parse_daily_sections(text)
    titles = [t for t, _ in sections]
    bodies = [b for _, b in sections]
    assert titles == ["", "Observation A", "Observation B"]
    assert "pre-heading preamble" in bodies[0]
    assert "body A" in bodies[1]
    assert "body B" in bodies[2]


@pytest.mark.asyncio
async def test_librarian_promotes_yesterdays_sections(tmp_path: Path) -> None:
    """End-to-end: daily file with one long section → one canonical write."""
    store = MemoryStore(store_dir=tmp_path)
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    body = (
        "This is a durable observation that should be promoted — the body is "
        "well over the trivial-body threshold of 80 chars, contains no request-echo "
        "or turn-summary patterns, and represents a fact worth keeping."
    )
    (tmp_path / "daily" / f"{yesterday}.md").write_text(
        f"# {yesterday}\n\n## [reference] Durable Fact\n\n{body}\n",
        encoding="utf-8",
    )

    librarian = Librarian(store=store)  # no embeddings — dedupe fails open
    result = await librarian.run_pass()

    assert result["promoted"] == 1, f"expected 1 promotion, got {result}"
    assert result["deduped"] == 0
    # The section should now exist in one of the canonical subdirs.
    references = list((tmp_path / "reference").glob("mem_*.md"))
    assert len(references) == 1, f"expected 1 reference file, got {references}"
    assert body[:50] in references[0].read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_librarian_skips_todays_daily_file(tmp_path: Path) -> None:
    """Today's file is still being appended to — librarian must not touch it."""
    store = MemoryStore(store_dir=tmp_path)
    today = date.today().isoformat()
    body = (
        "Another durable observation longer than the 80-char threshold so it "
        "would normally be promoted if it were in yesterday's file."
    )
    (tmp_path / "daily" / f"{today}.md").write_text(
        f"# {today}\n\n## Today fact\n\n{body}\n",
        encoding="utf-8",
    )
    librarian = Librarian(store=store)
    result = await librarian.run_pass()
    assert result["promoted"] == 0, "today's daily must be skipped"


@pytest.mark.asyncio
async def test_librarian_skips_trivial_body_sections(tmp_path: Path) -> None:
    store = MemoryStore(store_dir=tmp_path)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    (tmp_path / "daily" / f"{yesterday}.md").write_text(
        f"# {yesterday}\n\n## Tiny\n\nshort.\n",
        encoding="utf-8",
    )
    librarian = Librarian(store=store)
    result = await librarian.run_pass()
    assert result["promoted"] == 0
    assert result["skipped"] >= 1


def test_memory_bundle_exposes_librarian() -> None:
    """build_memory_bundle must attach a live Librarian to the bundle.

    Regression guard: `_cmd_reflect` reads `app["memory_bundle"].librarian`.
    """
    from tesseract.brain.boot import build_memory_bundle

    bundle = build_memory_bundle()
    assert bundle.librarian is not None
    assert isinstance(bundle.librarian, Librarian)


@pytest.mark.asyncio
async def test_librarian_second_pass_is_idempotent_without_embeddings(tmp_path: Path) -> None:
    """Audit-2 finding #2 — repeat `/reflect` runs without embeddings must not
    re-promote the same daily section. The source-path anchor guard closes
    that gap before the cosine-similarity dedupe runs."""
    store = MemoryStore(store_dir=tmp_path)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    body = (
        "A durable section that should only land once — well over 80 chars, "
        "no request-echo, stable content so we can test repeat-pass idempotence."
    )
    (tmp_path / "daily" / f"{yesterday}.md").write_text(
        f"# {yesterday}\n\n## [reference] Durable\n\n{body}\n",
        encoding="utf-8",
    )

    librarian = Librarian(store=store)  # no embeddings on purpose
    first = await librarian.run_pass()
    second = await librarian.run_pass()

    assert first["promoted"] == 1
    assert second["promoted"] == 0, f"second pass re-promoted: {second}"
    assert second["deduped"] >= 1
    assert len(list((tmp_path / "reference").glob("mem_*.md"))) == 1


@pytest.mark.asyncio
async def test_memory_save_dedupe_refresh_reports_error_on_blocked_rewrite(
    tmp_path: Path,
) -> None:
    """Audit-2 finding #4 — when a dedupe hit's rewrite is blocked by
    WhatNotToSave, the tool must surface `is_error=True` instead of
    silently reporting success."""
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock

    from tesseract.kernel.tools.memory_save import MemorySaveTool
    from tesseract.memory import dedupe as dedupe_module
    from tesseract.memory.index import MemoryIndex
    from tesseract.memory.types import MemoryFrontmatter, MemoryType

    store = MemoryStore(store_dir=tmp_path)
    index = MemoryIndex(store_dir=tmp_path)

    existing_fm = MemoryFrontmatter(
        id=MemoryFrontmatter.generate_id(),
        type=MemoryType.REFERENCE,
        title="stale",
        summary="",
        created_at=datetime.now(timezone.utc),
        updated_at=None,
        importance=5,
        tags=[],
        source_session="t",
        source_path="",
        source_url="",
        source_type="",
    )
    seed_body = (
        "TESSERACT is the runtime TARS runs on; operator uses Windows + WSL for "
        "daily work with the assistant — durable content well over eighty chars."
    )
    assert store.write(existing_fm, seed_body)

    class _FakeEmbeddings:
        add = AsyncMock()
        search = AsyncMock(return_value=[])
    tool = MemorySaveTool(store=store, index=index, embeddings=_FakeEmbeddings())

    async def _fake_check(_body: str, _emb: object) -> tuple[bool, str]:
        return (False, existing_fm.id)
    original = dedupe_module.check
    dedupe_module.check = _fake_check
    try:
        def _blocker(body: str) -> bool:
            return False
        store._wnts.should_save = _blocker  # type: ignore[method-assign]
        result = await tool._refresh_existing(existing_fm.id, datetime.now(timezone.utc))
    finally:
        dedupe_module.check = original

    assert result.is_error is True, "blocked rewrite must surface as error"
    assert "refresh rejected" in result.output.lower() or "not persisted" in result.output.lower()


@pytest.mark.asyncio
async def test_memory_save_invokes_dedupe_check(tmp_path: Path) -> None:
    """MemorySaveTool must call dedupe.check before writing when embeddings are live.

    Regression guard for the F1 runtime gap — dedupe.check had no live caller
    at the time of audit.
    """
    from unittest.mock import AsyncMock

    from tesseract.kernel.tools.base import ToolContext
    from tesseract.kernel.tools.memory_save import MemorySaveInput, MemorySaveTool
    from tesseract.memory.index import MemoryIndex

    store = MemoryStore(store_dir=tmp_path)
    index = MemoryIndex(store_dir=tmp_path)

    class _FakeEmbeddings:
        add = AsyncMock()
        search = AsyncMock(return_value=[])  # no dupes — write proceeds

    fake_embeddings = _FakeEmbeddings()
    tool = MemorySaveTool(store=store, index=index, embeddings=fake_embeddings)

    body = (
        "A durable observation over 80 chars — testing that dedupe.search is "
        "actually invoked on the save path."
    )
    ctx = ToolContext(workspace_root=str(tmp_path), session_id="t", cli_sink=None)
    result = await tool.run(MemorySaveInput(type="reference", title="Dedupe wiring", content=body), ctx)
    assert not result.is_error, result.output
    fake_embeddings.search.assert_awaited()  # dedupe.check → embeddings.search
