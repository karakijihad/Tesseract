"""Stage-0 multi-word phrase normalization regression (workshop note 2026-05-01).

Covers the exact-lookup tightening for people-profile retrieval:

- multi-word entity (e.g. "John Doe") matches a phrase query
- punctuation/case in the query do not block exact entity match
- slug match still wins via short-circuit when the query embeds the slug
- expired memory does not surface even if it would phrase-match
- subfolder records (reference/people/<id>.md) are still discoverable
- two records with the same entity surface together, both tagged ambiguous
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from tesseract.memory.fts_index import FTSIndex
from tesseract.memory.index import MemoryIndex
from tesseract.memory.retrieval import RetrievalPipeline
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType


def _bundle(tmp_path: Path) -> tuple[MemoryStore, MemoryIndex, FTSIndex, RetrievalPipeline]:
    store_dir = tmp_path / "memory-store"
    (store_dir / "derived").mkdir(parents=True, exist_ok=True)
    store = MemoryStore(store_dir=store_dir)
    index = MemoryIndex(store_dir=store_dir)
    fts = FTSIndex(db_path=store_dir / "derived" / "fts.db")
    pipeline = RetrievalPipeline(store=store, index=index, embeddings=None, fts_index=fts)
    return store, index, fts, pipeline


def _seed(
    store: MemoryStore,
    fts: FTSIndex,
    *,
    mem_id: str,
    title: str,
    body: str,
    slug: str = "",
    entities: list[str] | None = None,
    expiry_at: datetime | None = None,
    mem_type: MemoryType = MemoryType.REFERENCE,
) -> MemoryFrontmatter:
    now = datetime.now(timezone.utc)
    fm = MemoryFrontmatter(
        id=mem_id,
        type=mem_type,
        title=title,
        summary=body[:80],
        tags=[],
        entities=entities or [],
        importance=7,
        created_at=now,
        updated_at=now,
        slug=slug,
        expiry_at=expiry_at,
    )
    assert store.write(fm, body)
    fts.add(fm.id, fm.title, body)
    return fm


def _move_to_subpath(
    store: MemoryStore,
    subpath: str,
    fm: MemoryFrontmatter,
) -> Path:
    """Move a freshly-written record to a subfolder under the store
    (e.g. reference/people/) to simulate the curated layout."""
    target = store.store_dir / subpath
    target.parent.mkdir(parents=True, exist_ok=True)
    flat = store.store_dir / fm.type.value / f"{fm.id}.md"
    flat.rename(target)
    return target


def test_multi_word_entity_matches_natural_phrase(tmp_path: Path) -> None:
    """Entity 'John Doe' must surface for natural-language queries that embed
    the full name with punctuation, stopwords, and varying case."""
    store, _index, fts, pipeline = _bundle(tmp_path)
    _seed(
        store, fts,
        mem_id="mem_john_doe_1",
        title="Profile: John Doe",
        body="A test profile used to verify multi-word entity phrase matching in Stage 0 of the retrieval pipeline.",
        entities=["John Doe"],
    )
    queries = [
        "who is John Doe?",
        "tell me about john doe",
        "john doe",
        "do we know anything about JOHN DOE today",
    ]
    for q in queries:
        hits, _short = pipeline.stage_zero_exact(q)
        ids = {r.memory_id for r in hits}
        assert "mem_john_doe_1" in ids, f"phrase entity match failed for query: {q!r}"
        hit = next(r for r in hits if r.memory_id == "mem_john_doe_1")
        assert "exact_entity" in hit.provenance


def test_slug_short_circuit_survives_normalization(tmp_path: Path) -> None:
    """Pre-existing canonical-decision lookup keeps working after the
    normalization rewrite — punctuation around the slug must not block the
    short-circuit (compatibility with test_belief_state)."""
    store, _index, fts, pipeline = _bundle(tmp_path)
    _seed(
        store, fts,
        mem_id="mem_voice_default",
        title="Voice default",
        body="Conversational by default. Operator-set canonical decision for the voice subsystem.",
        slug="voice_default",
        mem_type=MemoryType.PROJECT,
    )
    hits, short_circuit = pipeline.stage_zero_exact("what was the voice_default decision?")
    assert short_circuit is True
    assert {r.memory_id for r in hits} == {"mem_voice_default"}
    assert hits[0].provenance == ("exact_slug",)


def test_phrase_match_does_not_resurrect_expired(tmp_path: Path) -> None:
    """Expired records must not surface even on a perfect entity phrase match.
    Stage 0 expiry filter has to run before phrase scoring."""
    store, _index, fts, pipeline = _bundle(tmp_path)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    _seed(
        store, fts,
        mem_id="mem_old_person",
        title="Old contact",
        body="An expired profile that should never surface even on a perfect phrase match against the entity.",
        entities=["Jane Doe"],
        expiry_at=past,
    )
    hits, _short = pipeline.stage_zero_exact("tell me about Jane Doe")
    assert "mem_old_person" not in {r.memory_id for r in hits}


def test_subfolder_record_discoverable_via_phrase(tmp_path: Path) -> None:
    """A people-profile saved under reference/people/<id>.md must be reachable
    through Stage 0 entity phrase match — not only flat reference/<id>.md."""
    store, _index, fts, pipeline = _bundle(tmp_path)
    fm = _seed(
        store, fts,
        mem_id="mem_subfolder_jane",
        title="Profile in subfolder",
        body="Profile placed under reference/people/ to simulate the curated people directory layout.",
        entities=["Jane Doe"],
    )
    _move_to_subpath(store, "reference/people/mem_subfolder_jane.md", fm)
    hits, _short = pipeline.stage_zero_exact("anything on Jane Doe?")
    assert "mem_subfolder_jane" in {r.memory_id for r in hits}


def test_ambiguous_entity_surfaces_both_with_provenance_tag(tmp_path: Path) -> None:
    """Two records sharing an entity must both surface, both labeled
    `exact_ambiguous` so the formatter can disambiguate instead of pretending
    one is correct (workshop note §3)."""
    store, _index, fts, pipeline = _bundle(tmp_path)
    _seed(
        store, fts,
        mem_id="mem_john_doe_engineer",
        title="John Doe — engineer",
        body="The engineer John Doe — works on backend systems and would never touch the frontend if avoidable.",
        entities=["John Doe"],
    )
    _seed(
        store, fts,
        mem_id="mem_john_doe_designer",
        title="John Doe — designer",
        body="The designer John Doe — focuses on visual systems and motion language for the product surface.",
        entities=["John Doe"],
    )
    hits, short_circuit = pipeline.stage_zero_exact("who is John Doe?")
    ids = {r.memory_id for r in hits}
    assert "mem_john_doe_engineer" in ids
    assert "mem_john_doe_designer" in ids
    for r in hits:
        assert "exact_entity" in r.provenance
        assert "exact_ambiguous" in r.provenance, (
            f"hit {r.memory_id} missing ambiguity tag; got {r.provenance}"
        )
    # Entity-only ambiguity does not short-circuit (B+D still allowed to add context).
    assert short_circuit is False


def test_two_unrelated_entity_hits_are_not_flagged_ambiguous(tmp_path: Path) -> None:
    """Two records matching DIFFERENT entities mentioned in the same query
    are not ambiguous — ambiguity means same-identity collision, not
    multi-record relevance."""
    store, _index, fts, pipeline = _bundle(tmp_path)
    _seed(
        store, fts,
        mem_id="mem_jane_doe",
        title="Profile: Jane Doe",
        body="An unrelated profile used to verify that hitting two different entities does not trigger the ambiguity tag.",
        entities=["Jane Doe"],
    )
    _seed(
        store, fts,
        mem_id="mem_john_doe",
        title="Profile: John Doe",
        body="Another unrelated profile used to verify that hitting two different entities does not trigger the ambiguity tag.",
        entities=["John Doe"],
    )
    hits, _short = pipeline.stage_zero_exact("compare Jane Doe and John Doe")
    ids = {r.memory_id for r in hits}
    assert ids == {"mem_jane_doe", "mem_john_doe"}
    for r in hits:
        assert "exact_entity" in r.provenance
        assert "exact_ambiguous" not in r.provenance, (
            f"hit {r.memory_id} falsely flagged ambiguous; got {r.provenance}"
        )
