"""Regression suite for memory-retune M4 — dedup tuning.

Covers:
  * `check_with_title` title-exact short-circuit blocks before cosine.
  * `check_with_title` title-fuzzy ratio > 0.85 blocks before cosine.
  * Title miss + no embeddings → proceed.
  * Cosine score in [0.88, 0.92) → "cosine_merge".
  * Cosine score >= 0.92 → "cosine_skip".
  * `Librarian._promote_daily` increments `merged`, bumps `updated_at`,
    and replaces the stored body on a cosine-merge hit.
  * Legacy `dedupe.check` signature preserved for `memory_save` backcompat.

Contract: `Docs/Plan/memory-retune/phase-m4-dedup-tuning.md`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from tesseract.memory import dedupe
from tesseract.memory.librarian import Librarian
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType


# ── Helpers ────────────────────────────────────────────────────────────────


class _FakeEmbeddings:
    """Minimal EmbeddingIndex stub — only `search()` is exercised."""

    def __init__(self, hits: list[tuple[str, float]]) -> None:
        self._hits = hits

    async def search(self, query: str, top_k: int = 1):
        return self._hits[:top_k]


def _seed_memory(
    store: MemoryStore,
    *,
    title: str,
    body: str,
    mem_type: MemoryType = MemoryType.USER,
    importance: int = 5,
    created_at: datetime | None = None,
) -> MemoryFrontmatter:
    now = created_at or datetime.now(timezone.utc)
    fm = MemoryFrontmatter(
        id=MemoryFrontmatter.generate_id(),
        type=mem_type,
        title=title,
        summary=body[:100],
        created_at=now,
        updated_at=now,
        importance=importance,
    )
    assert store.write(fm, body) is True
    return fm


def _seed_daily(tmp_path: Path, date_stem: str, sections: list[tuple[str, str]]) -> Path:
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    path = daily_dir / f"{date_stem}.md"
    parts: list[str] = [f"# {date_stem}", ""]
    for title, body in sections:
        parts.append(f"## {title}")
        parts.append(body)
        parts.append("")
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


_LONG_BODY_A = (
    "The operator runs two workstations. The dev box holds 8GB of VRAM and "
    "the Dell holds 6GB. Models rotate between them depending on workload."
)
_LONG_BODY_B = (
    "Operator maintains two workstations with 8GB VRAM (dev) and 6GB VRAM "
    "(Dell); model rotation depends on which card is free at the time."
)
_DIFFERENT_BODY = (
    "FAISS periodic rebuild hook is implemented via rebuild_from_store and "
    "can fire from the dreaming heartbeat once wiring lands."
)


# ── check_with_title ──────────────────────────────────────────────────────


async def test_title_exact_match_blocks(tmp_path: Path) -> None:
    store = MemoryStore(store_dir=tmp_path)
    existing = _seed_memory(store, title="Operator hardware", body=_LONG_BODY_A)

    proceed, matched_id, reason = await dedupe.check_with_title(
        "Operator hardware",
        _DIFFERENT_BODY,
        store,
        embeddings=None,
    )

    assert proceed is False
    assert matched_id == existing.id
    assert reason == "title_exact"


async def test_title_exact_ignores_type_prefix(tmp_path: Path) -> None:
    """A `[user] Foo` daily title must collide with a stored `Foo` memory."""
    store = MemoryStore(store_dir=tmp_path)
    existing = _seed_memory(store, title="Operator hardware", body=_LONG_BODY_A)

    proceed, matched_id, reason = await dedupe.check_with_title(
        "[user] Operator hardware",
        _DIFFERENT_BODY,
        store,
        embeddings=None,
    )

    assert proceed is False
    assert matched_id == existing.id
    assert reason == "title_exact"


async def test_title_fuzzy_match_blocks(tmp_path: Path) -> None:
    store = MemoryStore(store_dir=tmp_path)
    existing = _seed_memory(store, title="Operator dev hardware", body=_LONG_BODY_A)

    # "Operator dev hardware note" vs "Operator dev hardware" — ratio > 0.85.
    proceed, matched_id, reason = await dedupe.check_with_title(
        "Operator dev hardware note",
        _DIFFERENT_BODY,
        store,
        embeddings=None,
    )

    assert proceed is False
    assert matched_id == existing.id
    assert reason == "title_fuzzy"


async def test_title_no_match_proceeds(tmp_path: Path) -> None:
    store = MemoryStore(store_dir=tmp_path)
    _seed_memory(store, title="Operator hardware", body=_LONG_BODY_A)

    proceed, matched_id, reason = await dedupe.check_with_title(
        "Completely unrelated topic about FAISS",
        _DIFFERENT_BODY,
        store,
        embeddings=None,
    )

    assert proceed is True
    assert matched_id is None
    assert reason is None


# Titles with zero token overlap so the fuzzy guard can't short-circuit
# the cosine cases below.
_SEED_TITLE = "Zeppelin industry dossier"
_FRESH_TITLE = "Aardvark foraging patterns"


async def test_cosine_merge_threshold(tmp_path: Path) -> None:
    store = MemoryStore(store_dir=tmp_path)
    existing = _seed_memory(store, title=_SEED_TITLE, body=_LONG_BODY_A)

    embeddings = _FakeEmbeddings(hits=[(existing.id, 0.90)])
    proceed, matched_id, reason = await dedupe.check_with_title(
        _FRESH_TITLE,
        _LONG_BODY_B,
        store,
        embeddings=embeddings,
    )

    assert proceed is False
    assert matched_id == existing.id
    assert reason == "cosine_merge"


async def test_cosine_skip_threshold(tmp_path: Path) -> None:
    store = MemoryStore(store_dir=tmp_path)
    existing = _seed_memory(store, title=_SEED_TITLE, body=_LONG_BODY_A)

    embeddings = _FakeEmbeddings(hits=[(existing.id, 0.93)])
    proceed, matched_id, reason = await dedupe.check_with_title(
        _FRESH_TITLE,
        _LONG_BODY_B,
        store,
        embeddings=embeddings,
    )

    assert proceed is False
    assert matched_id == existing.id
    assert reason == "cosine_skip"


async def test_cosine_below_merge_proceeds(tmp_path: Path) -> None:
    store = MemoryStore(store_dir=tmp_path)
    existing = _seed_memory(store, title=_SEED_TITLE, body=_LONG_BODY_A)

    embeddings = _FakeEmbeddings(hits=[(existing.id, 0.40)])
    proceed, matched_id, reason = await dedupe.check_with_title(
        _FRESH_TITLE,
        _DIFFERENT_BODY,
        store,
        embeddings=embeddings,
    )

    assert proceed is True
    assert matched_id is None
    assert reason is None


# ── Librarian integration ──────────────────────────────────────────────────


async def test_librarian_merged_counter(tmp_path: Path) -> None:
    """Daily section with unique title but cosine-merge hit → existing body
    replaced, `updated_at` bumped, `merged=1`, `promoted=0`."""
    store = MemoryStore(store_dir=tmp_path)
    past = datetime.now(timezone.utc) - timedelta(days=2)
    existing = _seed_memory(
        store,
        title=_SEED_TITLE,
        body=_LONG_BODY_A,
        created_at=past,
    )

    _seed_daily(tmp_path, "2024-01-02", [
        (f"[user] {_FRESH_TITLE}", _LONG_BODY_B),
    ])

    embeddings = _FakeEmbeddings(hits=[(existing.id, 0.90)])
    librarian = Librarian(store=store, embeddings=embeddings, adapter=None)
    result = await librarian.run_pass()

    assert result["promoted"] == 0
    assert result["merged"] == 1
    assert result["deduped"] == 0

    reread = store.read(existing.id, log_access=False)
    assert reread is not None
    fm_after, body_after = reread
    assert body_after == _LONG_BODY_B, "existing body must be replaced by new body"
    assert fm_after.updated_at is not None
    assert fm_after.updated_at > past, "updated_at must be bumped on merge"


async def test_librarian_cosine_skip_not_merged(tmp_path: Path) -> None:
    """Score >=0.92 increments `deduped`, not `merged`; body untouched."""
    store = MemoryStore(store_dir=tmp_path)
    past = datetime.now(timezone.utc) - timedelta(days=2)
    existing = _seed_memory(
        store,
        title=_SEED_TITLE,
        body=_LONG_BODY_A,
        created_at=past,
    )

    _seed_daily(tmp_path, "2024-01-03", [
        (f"[user] {_FRESH_TITLE}", _LONG_BODY_B),
    ])

    embeddings = _FakeEmbeddings(hits=[(existing.id, 0.95)])
    librarian = Librarian(store=store, embeddings=embeddings, adapter=None)
    result = await librarian.run_pass()

    assert result["merged"] == 0
    assert result["deduped"] == 1
    assert result["promoted"] == 0

    reread = store.read(existing.id, log_access=False)
    assert reread is not None
    _, body_after = reread
    assert body_after == _LONG_BODY_A, "cosine_skip must not touch the existing body"


# ── Backward compat — `memory_save` path ───────────────────────────────────


async def test_dedupe_check_backward_compat() -> None:
    """Legacy `dedupe.check(body, emb, threshold)` signature preserved."""
    emb = _FakeEmbeddings(hits=[("mem_abc123", 0.95)])
    ok, existing = await dedupe.check("some body", emb, threshold=0.92)
    assert ok is False
    assert existing == "mem_abc123"

    empty = _FakeEmbeddings(hits=[])
    ok2, existing2 = await dedupe.check("unrelated body", empty)
    assert ok2 is True
    assert existing2 is None
