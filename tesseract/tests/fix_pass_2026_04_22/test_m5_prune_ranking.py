"""Regression suite for memory-retune M5 — ranking.

Covers:
  * `librarian._is_bookkeeping_entry` catches title-prefixed bookkeeping
    AND non-prefixed REFERENCE stubs from the librarian (`daily:*` tag).
  * `Librarian._top_by_importance` returns `(top, filtered)` with the
    right priority ordering and excludes tag-based bookkeeping.
  * `Librarian.run_pass()` forwards `filtered` into the log line.

Plan: `Docs/Plan/memory-retune/phase-m5-prune-ranking.md`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from tesseract.memory.librarian import (
    Librarian,
    _is_bookkeeping_entry,
)
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType


NOW = datetime(2026, 4, 22, 16, 0, tzinfo=timezone.utc)


# ── Helpers ────────────────────────────────────────────────────────────────


def _fm(
    *,
    mem_id: str,
    mem_type: MemoryType,
    title: str,
    importance: int = 5,
    created_at: datetime = NOW,
    tags: list[str] | None = None,
    source_session: str = "test",
) -> MemoryFrontmatter:
    return MemoryFrontmatter(
        id=mem_id,
        type=mem_type,
        title=title,
        summary=title[:50],
        created_at=created_at,
        updated_at=created_at,
        importance=importance,
        tags=tags or [],
        entities=[],
        links=[],
        auto_links=[],
        source_session=source_session,
        source_path="",
        source_url="",
        source_type="test",
    )


def _seed_store(tmp_path: Path, entries: list[MemoryFrontmatter]) -> MemoryStore:
    store_dir = tmp_path / "memory-store"
    store = MemoryStore(store_dir=store_dir)
    for fm in entries:
        body = f"# {fm.title}\n\n" + ("body content long enough to clear the librarian floor — " * 3)
        assert store.write(fm, body) is True
    return store


# ── _is_bookkeeping_entry ──────────────────────────────────────────────────


def test_bookkeeping_entry_matches_title_prefix():
    fm = _fm(mem_id="mem_a", mem_type=MemoryType.REFERENCE, title="[reflect] Reflection sess-2")
    assert _is_bookkeeping_entry(fm) is True


def test_bookkeeping_entry_matches_daily_librarian_reference():
    # Post audit-1 (2026-04-24) M5: librarian-promoted REFERENCE entries are
    # legitimate content unless they carry a bookkeeping title-prefix OR an
    # explicit `bookkeeping` tag. A bare `daily:*` tag no longer implies
    # bookkeeping — `[chat_digest]` and `[reference]` promotions belong in
    # MEMORY.md.
    fm = _fm(
        mem_id="mem_b",
        mem_type=MemoryType.REFERENCE,
        title="Legacy promoted note without bracket",
        tags=["promoted", "daily:2026-04-20", "bookkeeping"],
        source_session="librarian",
    )
    assert _is_bookkeeping_entry(fm) is True


def test_bookkeeping_entry_keeps_chat_digest_promotion():
    """A `[chat_digest]` REFERENCE from the librarian must surface in MEMORY.md."""
    fm = _fm(
        mem_id="mem_digest",
        mem_type=MemoryType.REFERENCE,
        title="[chat_digest] 2026-04-22",
        tags=["promoted", "daily:2026-04-22", "chat_digest"],
        source_session="librarian",
    )
    assert _is_bookkeeping_entry(fm) is False


def test_bookkeeping_entry_keeps_reference_promotion():
    """A `[reference]` promotion from classifier must not be filtered out."""
    fm = _fm(
        mem_id="mem_ref",
        mem_type=MemoryType.REFERENCE,
        title="[reference] Citation — Karpathy gist",
        tags=["promoted", "daily:2026-04-22"],
        source_session="librarian",
    )
    assert _is_bookkeeping_entry(fm) is False


def test_bookkeeping_entry_keeps_operator_reference():
    fm = _fm(
        mem_id="mem_c",
        mem_type=MemoryType.REFERENCE,
        title="Operator-saved research note",
        tags=["research"],
        source_session="memory_save",
    )
    assert _is_bookkeeping_entry(fm) is False


def test_bookkeeping_entry_keeps_librarian_user_even_with_daily_tag():
    # Only REFERENCE gets the tag-based filter — USER/FEEDBACK/PROJECT are
    # always kept even when the librarian promoted them via a daily tag.
    fm = _fm(
        mem_id="mem_d",
        mem_type=MemoryType.USER,
        title="Operator preference discovered in daily log",
        tags=["promoted", "daily:2026-04-20"],
        source_session="librarian",
    )
    assert _is_bookkeeping_entry(fm) is False


# ── _top_by_importance + filtered counter ──────────────────────────────────


def test_top_by_importance_returns_tuple_with_filtered(tmp_path: Path):
    entries = [
        _fm(mem_id="mem_user", mem_type=MemoryType.USER, title="real user", importance=3),
        _fm(mem_id="mem_ref_stub", mem_type=MemoryType.REFERENCE, title="[reflect] stub", importance=9),
    ]
    store = _seed_store(tmp_path, entries)
    librarian = Librarian(store=store, embeddings=None)

    top, filtered = librarian._top_by_importance(limit=10)
    assert [fm.title for fm in top] == ["real user"]
    assert filtered == 1


def test_top_by_importance_filters_tag_based_reference(tmp_path: Path):
    # Explicit `bookkeeping` tag still filters the entry from Top-N.
    # Post audit-1 M5, a bare `daily:*` tag is NOT sufficient — the prune
    # script (or future migration tools) must mark entries explicitly.
    entries = [
        _fm(mem_id="mem_user", mem_type=MemoryType.USER, title="real user", importance=3),
        _fm(
            mem_id="mem_ref_tag",
            mem_type=MemoryType.REFERENCE,
            title="Promoted legacy stub",
            importance=10,
            tags=["daily:2026-04-21", "bookkeeping"],
            source_session="librarian",
        ),
    ]
    store = _seed_store(tmp_path, entries)
    librarian = Librarian(store=store, embeddings=None)

    top, filtered = librarian._top_by_importance(limit=10)
    assert [fm.title for fm in top] == ["real user"]
    assert filtered == 1


def test_top_by_importance_user_beats_high_importance_reference(tmp_path: Path):
    entries = [
        _fm(mem_id="mem_user", mem_type=MemoryType.USER, title="user low importance", importance=3),
        _fm(mem_id="mem_ref", mem_type=MemoryType.REFERENCE, title="ref high importance", importance=9),
    ]
    store = _seed_store(tmp_path, entries)
    librarian = Librarian(store=store, embeddings=None)

    top, filtered = librarian._top_by_importance(limit=10)
    assert top[0].title == "user low importance"
    assert top[1].title == "ref high importance"
    assert filtered == 0


async def test_run_pass_log_line_includes_filtered(tmp_path: Path, caplog):
    entries = [
        _fm(mem_id="mem_user", mem_type=MemoryType.USER, title="real user"),
        _fm(mem_id="mem_ref_stub", mem_type=MemoryType.REFERENCE, title="[reflect] stub"),
        _fm(
            mem_id="mem_ref_tag",
            mem_type=MemoryType.REFERENCE,
            title="Non-bracketed librarian stub",
            # Explicit `bookkeeping` tag: survives audit-1 M5 filter split.
            tags=["daily:2026-04-20", "bookkeeping"],
            source_session="librarian",
        ),
    ]
    store = _seed_store(tmp_path, entries)
    librarian = Librarian(store=store, embeddings=None)

    with caplog.at_level(logging.INFO, logger="tesseract.memory.librarian"):
        result = await librarian.run_pass()

    assert result["filtered"] == 2
    assert any("filtered=2" in rec.message for rec in caplog.records)
