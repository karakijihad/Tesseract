"""CR-1: SQLite + FTS5 work-history index — schema, insert, search.

Mirrors ``tesseract/memory/fts_index.py`` patterns: WAL, BM25 ranking,
graceful failure. Chunks carry provenance (``session:`` / ``workshop:``)
that retrieval never auto-promotes to authoritative memory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.memory.work_index import WorkIndex, WorkChunk


def test_db_created_with_wal_mode(tmp_path: Path) -> None:
    idx = WorkIndex(tmp_path / "work.sqlite")
    try:
        cursor = idx._conn.execute("PRAGMA journal_mode")  # noqa: SLF001
        mode = cursor.fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        idx.close()


def test_add_and_search_session_chunk(tmp_path: Path) -> None:
    idx = WorkIndex(tmp_path / "work.sqlite")
    idx.add(WorkChunk(
        source="session",
        source_path="tesseract/sessions/2026-05-22.json",
        source_ref="sess-abc",
        turn_idx=4,
        role="user",
        chunk_idx=0,
        ts="2026-05-22T10:00:00+00:00",
        text="hermes comparison and workshop indexing discussion",
    ))
    results = idx.search("workshop indexing", top_k=5)
    assert len(results) == 1
    hit = results[0]
    assert hit.source == "session"
    assert hit.source_ref == "sess-abc"
    assert hit.turn_idx == 4
    assert "workshop indexing" in hit.text


def test_add_and_search_workshop_chunk(tmp_path: Path) -> None:
    idx = WorkIndex(tmp_path / "work.sqlite")
    idx.add(WorkChunk(
        source="workshop",
        source_path="tesseract/tars-workshop/2026-05-22/entity-autonomy-plan/README.md",
        source_ref="entity-autonomy-plan",
        turn_idx=None,
        role=None,
        chunk_idx=0,
        ts="2026-05-22T08:00:00+00:00",
        text="Entity autonomy plan — Phase 1 vertical slice with governed delegation.",
    ))
    results = idx.search("entity autonomy", top_k=5)
    assert len(results) == 1
    assert results[0].source == "workshop"
    assert results[0].source_ref == "entity-autonomy-plan"
    assert results[0].turn_idx is None


def test_search_filters_by_source(tmp_path: Path) -> None:
    idx = WorkIndex(tmp_path / "work.sqlite")
    idx.add(WorkChunk(source="session", source_path="a.json", source_ref="s1",
                      turn_idx=0, role="user", chunk_idx=0, ts="2026-05-22",
                      text="apples and oranges"))
    idx.add(WorkChunk(source="workshop", source_path="b.md", source_ref="b",
                      turn_idx=None, role=None, chunk_idx=0, ts="2026-05-22",
                      text="apples in workshop context"))
    session_only = idx.search("apples", source="session", top_k=10)
    workshop_only = idx.search("apples", source="workshop", top_k=10)
    assert {h.source for h in session_only} == {"session"}
    assert {h.source for h in workshop_only} == {"workshop"}
    assert idx.search("apples", source="both", top_k=10)


def test_search_filters_by_time_window(tmp_path: Path) -> None:
    idx = WorkIndex(tmp_path / "work.sqlite")
    idx.add(WorkChunk(source="session", source_path="a.json", source_ref="s",
                      turn_idx=0, role="user", chunk_idx=0,
                      ts="2026-04-01T10:00:00+00:00",
                      text="ancient hermes thread"))
    idx.add(WorkChunk(source="session", source_path="b.json", source_ref="t",
                      turn_idx=0, role="user", chunk_idx=0,
                      ts="2026-05-22T10:00:00+00:00",
                      text="recent hermes thread"))
    recent = idx.search("hermes", since="2026-05-01", top_k=10)
    assert {h.source_ref for h in recent} == {"t"}


def test_count_and_rebuild(tmp_path: Path) -> None:
    idx = WorkIndex(tmp_path / "work.sqlite")
    for i in range(5):
        idx.add(WorkChunk(source="session", source_path=f"a-{i}.json",
                          source_ref=f"s{i}", turn_idx=0, role="user",
                          chunk_idx=0, ts="2026-05-22",
                          text=f"chunk {i}"))
    assert idx.count() == 5
    # Rebuild clears.
    idx.rebuild([])
    assert idx.count() == 0


def test_search_empty_query_returns_empty(tmp_path: Path) -> None:
    idx = WorkIndex(tmp_path / "work.sqlite")
    idx.add(WorkChunk(source="session", source_path="a.json", source_ref="s",
                      turn_idx=0, role="user", chunk_idx=0, ts="2026-05-22",
                      text="text"))
    assert idx.search("", top_k=10) == []
    assert idx.search("   ", top_k=10) == []


def test_search_survives_punctuation_and_keywords(tmp_path: Path) -> None:
    """Regression: `_sanitize_query` must not crash or silently drop matches
    on operator-typed punctuation / FTS5 keyword words. Prior stripping
    approach split contractions, mangled hyphens, and let AND/OR/NOT act as
    boolean operators (2026-07-02 live-recall bug)."""
    idx = WorkIndex(tmp_path / "work.sqlite")
    idx.add(WorkChunk(
        source="session", source_path="a.json", source_ref="s1",
        turn_idx=0, role="user", chunk_idx=0, ts="2026-05-22",
        text="what is on your plate and now the foo bar review",
    ))
    try:
        # None of these may raise (prior code raised OperationalError):
        for q in ["What's on your plate?", "foo-bar", "plate AND now",
                  "don't stop", 'say "hi"', "NEAR(x)"]:
            idx.search(q, top_k=5)  # must not raise
        # AND semantics preserved: all terms present -> hit; a missing term -> none.
        assert idx.search("plate review", top_k=5), "both terms present should match"
        assert idx.search("plate zzzmissing", top_k=5) == [], "missing term drops the AND"
        # whitespace-only still short-circuits
        assert idx.search("   ", top_k=5) == []
    finally:
        idx.close()
