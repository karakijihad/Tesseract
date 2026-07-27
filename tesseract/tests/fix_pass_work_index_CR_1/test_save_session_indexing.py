"""M2 follow-up to CR-1: save_session indexes the JSON into the
work-history index automatically.

Operator-attended REPL + Mirror close paths both flow through
``save_session`` — hooking the indexer there covers both with one
call. Best-effort: a failed indexer never blocks the save.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tesseract.brain.session_store import save_session
from tesseract.memory.work_index import WorkIndex


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))


def test_save_session_populates_work_index(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    history = [
        {"role": "user", "content": "post-save indexing should work"},
        {"role": "assistant", "content": "right, hook lives in save_session"},
    ]
    path = save_session(
        sessions_dir,
        name="2026-05-22-test",
        model="fake",
        started_at="2026-05-22T10:00:00+00:00",
        history=history,
    )
    assert path.exists()
    # The fresh session must be queryable through the same WorkIndex DB
    # the runtime would use (TESSERACT_HOME/work_index.sqlite).
    idx = WorkIndex(tmp_path / "work_index.sqlite")
    hits = idx.search("post-save indexing", top_k=5)
    assert any(h.source_ref == "2026-05-22-test" for h in hits), (
        f"freshly-saved session not in work index: {[h.source_ref for h in hits]}"
    )


def test_save_session_is_idempotent_in_index(tmp_path: Path) -> None:
    """Saving the same session twice (e.g. on /save then on REPL exit)
    must not double-count chunks in the index."""
    sessions_dir = tmp_path / "sessions"
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
    save_session(
        sessions_dir,
        name="2026-05-22-dup",
        model="fake",
        started_at="2026-05-22T10:00:00+00:00",
        history=history,
    )
    save_session(
        sessions_dir,
        name="2026-05-22-dup",
        model="fake",
        started_at="2026-05-22T10:00:00+00:00",
        history=history,
    )
    idx = WorkIndex(tmp_path / "work_index.sqlite")
    # The two-message history yields exactly 2 chunks under the
    # idempotent per-path delete-then-add contract; doubling would
    # mean delete_by_path silently failed.
    assert idx.count() == 2, f"non-idempotent re-save left {idx.count()} rows"
    # Query each token individually — FTS5 default treats multi-word
    # queries as AND across all columns, but our chunks split the
    # phrase across two messages.
    hello_hits = idx.search("hello", top_k=10)
    world_hits = idx.search("world", top_k=10)
    assert len(hello_hits) == 1, (
        f"chunks duplicated on re-save (hello): {hello_hits}"
    )
    assert len(world_hits) == 1, (
        f"chunks duplicated on re-save (world): {world_hits}"
    )


def test_save_session_indexer_failure_does_not_block_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the indexer raises (DB locked, disk full, etc.), the save
    itself must still succeed — indexing is best-effort downstream."""
    import tesseract.brain.session_store as session_store

    def _boom(*a, **k):
        raise RuntimeError("simulated indexer crash")

    monkeypatch.setattr(session_store, "index_conversation_file", _boom)

    sessions_dir = tmp_path / "sessions"
    path = save_session(
        sessions_dir,
        name="2026-05-22-crash",
        model="fake",
        started_at="2026-05-22T10:00:00+00:00",
        history=[{"role": "user", "content": "save must succeed"}],
    )
    assert path.exists()
    assert "save must succeed" in path.read_text(encoding="utf-8")
