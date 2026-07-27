"""Regression suite for scheduler S2 — librarian heartbeat + index rebuild.

Covers: LibrarianHeartbeatJob happy path, missing bundle, raising librarian;
IndexRebuildJob embeddings offline (FTS-only), embeddings online (FAISS + FTS),
missing bundle.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from tesseract.scheduler.tasks.index_rebuild import IndexRebuildJob
from tesseract.scheduler.tasks.librarian_heartbeat import LibrarianHeartbeatJob
from tesseract.scheduler.types import JobContext


# ── helpers ───────────────────────────────────────────────────────────────


def _ctx(job_name: str, app) -> JobContext:
    return JobContext(
        job_name=job_name,
        fired_at=datetime(2026, 4, 21, 15, 0, tzinfo=timezone.utc),
        app=app,
    )


class _FakeStore:
    def __init__(self, entries: list[tuple[str, str, str]]) -> None:
        # entries: list of (id, title, body)
        self._entries = entries

    def list_all(self):
        return [SimpleNamespace(id=mid, title=title) for mid, title, _body in self._entries]

    def read(self, memory_id: str, log_access: bool = True):
        for mid, _title, body in self._entries:
            if mid == memory_id:
                fm = SimpleNamespace(id=mid)
                return fm, body
        return None


class _FakeFTSIndex:
    def __init__(self) -> None:
        self.last_rebuild: list[tuple[str, str, str]] | None = None

    def rebuild(self, memories):
        self.last_rebuild = list(memories)
        return len(self.last_rebuild)


class _FakeEmbeddings:
    def __init__(self) -> None:
        self.last_pairs: list[tuple[str, str]] | None = None

    async def rebuild_from_store(self, chunks):
        self.last_pairs = list(chunks)
        return len(self.last_pairs)


# ── LibrarianHeartbeatJob ─────────────────────────────────────────────────


async def test_librarian_heartbeat_happy_path():
    stats = {
        "promoted": 3,
        "deduped": 2,
        "skipped": 1,
        "counts": {"user": 5, "feedback": 4, "project": 2, "reference": 1},
        "top": 12,
        "recent": 5,
    }

    class _Librarian:
        async def run_pass(self):
            return stats

        async def distill_personality_candidates(self, soul_path):
            return {"candidates": 0, "reason": "stub"}

    bundle = SimpleNamespace(librarian=_Librarian())
    result = await LibrarianHeartbeatJob().run(_ctx("librarian_heartbeat", {"memory_bundle": bundle}))
    assert result.ok is True
    assert result.detail == "promoted=3 deduped=2 skipped=1 distilled=0"
    assert result.payload["promoted"] == 3
    assert result.payload["deduped"] == 2
    assert result.payload["skipped"] == 1
    assert result.payload["counts"]["user"] == 5
    assert result.payload["distilled"]["candidates"] == 0


async def test_librarian_heartbeat_missing_bundle():
    result = await LibrarianHeartbeatJob().run(_ctx("librarian_heartbeat", None))
    assert result.ok is False
    assert "memory_bundle" in result.detail


async def test_librarian_heartbeat_bundle_without_librarian():
    bundle = SimpleNamespace(librarian=None)
    result = await LibrarianHeartbeatJob().run(_ctx("librarian_heartbeat", {"memory_bundle": bundle}))
    assert result.ok is False
    assert "memory_bundle" in result.detail


async def test_librarian_heartbeat_raising_librarian():
    class _BrokenLibrarian:
        async def run_pass(self):
            raise RuntimeError("boom")

    bundle = SimpleNamespace(librarian=_BrokenLibrarian())
    result = await LibrarianHeartbeatJob().run(_ctx("librarian_heartbeat", {"memory_bundle": bundle}))
    assert result.ok is False
    assert "unhandled" in result.detail
    assert "RuntimeError" in result.detail
    assert "boom" in result.detail


# ── IndexRebuildJob ───────────────────────────────────────────────────────


async def test_index_rebuild_embeddings_offline():
    entries = [
        ("mem1", "first memory", "body one with enough content"),
        ("mem2", "second memory", "body two with enough content"),
    ]
    fts = _FakeFTSIndex()
    bundle = SimpleNamespace(store=_FakeStore(entries), embeddings=None, fts_index=fts)
    result = await IndexRebuildJob().run(_ctx("index_rebuild", {"memory_bundle": bundle}))
    assert result.ok is True
    assert result.payload["embedding_available"] is False
    assert result.payload["faiss_count"] == 0
    assert result.payload["fts_count"] == 2
    assert fts.last_rebuild == [
        ("mem1", "first memory", "body one with enough content"),
        ("mem2", "second memory", "body two with enough content"),
    ]
    assert "faiss=skipped" in result.detail
    assert "fts=2" in result.detail


async def test_index_rebuild_embeddings_online():
    entries = [
        ("mem1", "alpha", "body alpha"),
        ("mem2", "beta", "body beta"),
        ("mem3", "gamma", "body gamma"),
    ]
    fts = _FakeFTSIndex()
    embeddings = _FakeEmbeddings()
    bundle = SimpleNamespace(store=_FakeStore(entries), embeddings=embeddings, fts_index=fts)
    result = await IndexRebuildJob().run(_ctx("index_rebuild", {"memory_bundle": bundle}))
    assert result.ok is True
    assert result.payload["embedding_available"] is True
    assert result.payload["faiss_count"] == 3
    assert result.payload["fts_count"] == 3
    assert embeddings.last_pairs == [
        ("mem1", "body alpha"),
        ("mem2", "body beta"),
        ("mem3", "body gamma"),
    ]
    assert result.detail == "faiss=3 fts=3"


async def test_index_rebuild_missing_bundle():
    result = await IndexRebuildJob().run(_ctx("index_rebuild", None))
    assert result.ok is False
    assert "memory_bundle" in result.detail


async def test_index_rebuild_embeddings_raises():
    class _BrokenEmbeddings:
        async def rebuild_from_store(self, chunks):
            raise RuntimeError("ollama exploded")

    entries = [("mem1", "t", "b")]
    fts = _FakeFTSIndex()
    bundle = SimpleNamespace(store=_FakeStore(entries), embeddings=_BrokenEmbeddings(), fts_index=fts)
    result = await IndexRebuildJob().run(_ctx("index_rebuild", {"memory_bundle": bundle}))
    assert result.ok is False
    assert "unhandled" in result.detail
    assert "ollama exploded" in result.detail
