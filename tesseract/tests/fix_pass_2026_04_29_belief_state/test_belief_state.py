"""Belief-state memory regressions (spec.md §1, §2; 2026-04-29).

Covers the new memory capabilities:

- frontmatter round-trips slug / confidence / expiry_at
- exact-slug stage 0 short-circuits the rest of the pipeline
- exact-entity stage 0 promotes by token match without short-circuit
- expired memory drops out of stage_a_prefilter
- memory_save rejects a duplicate slug
- memory_search output includes provenance + confidence per hit
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.memory_save import MemorySaveInput, MemorySaveTool
from tesseract.kernel.tools.memory_search import MemorySearchInput, MemorySearchTool
from tesseract.memory.fts_index import FTSIndex
from tesseract.memory.index import MemoryIndex
from tesseract.memory.retrieval import RetrievalPipeline
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType


def _bundle(tmp_path: Path) -> tuple[MemoryStore, MemoryIndex, FTSIndex, RetrievalPipeline]:
    store_dir = tmp_path / "memory-store"
    derived_dir = store_dir / "derived"
    derived_dir.mkdir(parents=True, exist_ok=True)
    store = MemoryStore(store_dir=store_dir)
    index = MemoryIndex(store_dir=store_dir)
    fts = FTSIndex(db_path=derived_dir / "fts.db")
    pipeline = RetrievalPipeline(
        store=store,
        index=index,
        embeddings=None,
        fts_index=fts,
    )
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
    mem_type: MemoryType = MemoryType.PROJECT,
    confidence: float = 1.0,
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
        confidence=confidence,
        expiry_at=expiry_at,
    )
    assert store.write(fm, body)
    fts.add(fm.id, fm.title, body)
    return fm


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        workspace_root=str(tmp_path),
        session_id="test-session",
    )


def test_frontmatter_round_trips_belief_fields(tmp_path: Path) -> None:
    """slug / confidence / expiry_at survive a write -> read cycle."""
    store, _index, _fts, _pipeline = _bundle(tmp_path)
    expiry = datetime(2030, 1, 1, tzinfo=timezone.utc)
    fm = _seed(
        store, _fts,
        mem_id="mem_round_trip",
        title="Voice default decision",
        body="Conversational mode by default unless the operator explicitly asks otherwise. Charon timbre, male British tone.",
        slug="voice_default",
        entities=["voice", "mood"],
        expiry_at=expiry,
        confidence=0.85,
    )
    read = store.read(fm.id)
    assert read is not None
    fm_back, _body = read
    assert fm_back.slug == "voice_default"
    assert fm_back.confidence == pytest.approx(0.85)
    assert fm_back.expiry_at == expiry
    assert fm_back.entities == ["voice", "mood"]


def test_default_confidence_omitted_from_yaml(tmp_path: Path) -> None:
    """Memories with confidence=1.0 (default) must round-trip without the
    field on disk so older files stay byte-identical after re-save."""
    store, _index, _fts, _pipeline = _bundle(tmp_path)
    fm = _seed(
        store, _fts,
        mem_id="mem_default_conf",
        title="Plain memory",
        body="A plain memory without any of the new belief-state fields, used to confirm the YAML stays minimal on disk.",
    )
    raw = (store.store_dir / "project" / f"{fm.id}.md").read_text(encoding="utf-8")
    assert "confidence:" not in raw
    assert "expiry_at:" not in raw
    assert "slug:" not in raw


def test_stage_zero_slug_short_circuits(tmp_path: Path) -> None:
    """A query containing the slug returns the slug-bearing memory and
    skips B+D entirely (stages_run == ('0',))."""
    store, _index, fts, pipeline = _bundle(tmp_path)
    _seed(
        store, fts,
        mem_id="mem_voice_default",
        title="Voice default",
        body="Conversational mode by default unless asked otherwise. Operator-set canonical decision for the voice subsystem.",
        slug="voice_default",
    )
    _seed(
        store, fts,
        mem_id="mem_unrelated",
        title="Permission engine",
        body="Permission engine evaluates tool calls through a layered pipeline of deny rules, ask rules, and path validation.",
    )
    packet = asyncio.run(pipeline.retrieve("what was the voice_default decision?"))
    assert packet.short_circuited is True
    assert packet.stages_run == ("0",)
    assert len(packet.results) == 1
    assert packet.results[0].memory_id == "mem_voice_default"
    assert packet.results[0].provenance == ("exact_slug",)


def test_stage_zero_entity_promotes_without_short_circuit(tmp_path: Path) -> None:
    """An entity match is a softer signal — the result list gets the
    entity hit but B+D still run."""
    store, _index, fts, pipeline = _bundle(tmp_path)
    _seed(
        store, fts,
        mem_id="mem_voice",
        title="Voice setup notes",
        body="Voice runs through Charon timbre. The TTS provider is Gemini Flash with a male British tone prompt as seed.",
        entities=["charon"],
    )
    _seed(
        store, fts,
        mem_id="mem_other",
        title="Other thing",
        body="The charon system mentioned here has nothing to do with the voice timbre setting in this particular entry.",
    )
    packet = asyncio.run(pipeline.retrieve("charon"))
    assert packet.short_circuited is False
    assert "0" in packet.stages_run
    assert any(r.memory_id == "mem_voice" for r in packet.results)
    voice_hit = next(r for r in packet.results if r.memory_id == "mem_voice")
    assert "exact_entity" in voice_hit.provenance


def test_expired_memory_dropped_from_prefilter(tmp_path: Path) -> None:
    """Memories whose expiry_at is in the past must not surface."""
    store, _index, fts, pipeline = _bundle(tmp_path)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    future = datetime.now(timezone.utc) + timedelta(days=30)
    _seed(
        store, fts,
        mem_id="mem_expired",
        title="Stale fact about permissions",
        body="Permission policy used to be lenient before the lockdown rewrite. This memory should not surface from retrieval.",
        expiry_at=past,
    )
    _seed(
        store, fts,
        mem_id="mem_current",
        title="Current permission policy",
        body="Permission policy is strict by default with explicit ASK gates on outbound calls and file writes everywhere.",
        expiry_at=future,
    )
    packet = asyncio.run(pipeline.retrieve("permission policy"))
    ids = {r.memory_id for r in packet.results}
    assert "mem_expired" not in ids
    assert "mem_current" in ids


def test_expired_slug_does_not_short_circuit(tmp_path: Path) -> None:
    """Stage 0 must respect expiry_at too; stale canonical decisions should
    not bypass the regular expiry filter through exact slug lookup."""
    store, _index, fts, pipeline = _bundle(tmp_path)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    _seed(
        store, fts,
        mem_id="mem_expired_slug",
        title="Expired canonical decision",
        body="This expired canonical decision should not surface, even when the query names its slug exactly.",
        slug="expired_choice",
        expiry_at=past,
    )
    packet = asyncio.run(pipeline.retrieve("expired_choice"))
    assert packet.short_circuited is False
    assert "mem_expired_slug" not in {r.memory_id for r in packet.results}


def test_memory_save_rejects_duplicate_slug(tmp_path: Path) -> None:
    """Saving a second memory with an existing slug must error out."""
    store, index, fts, _pipeline = _bundle(tmp_path)
    _seed(
        store, fts,
        mem_id="mem_first",
        title="First decision",
        body="The very first decision recorded under this slug, claimed by the operator at the start of the day.",
        slug="canonical_choice",
    )
    tool = MemorySaveTool(store=store, index=index, fts_index=fts)
    inp = MemorySaveInput(
        type="project",
        title="Second decision",
        content="A second attempt to claim the same slug — should be blocked because canonical slugs must remain unique.",
        slug="canonical_choice",
    )
    result = asyncio.run(tool.run(inp, _ctx(tmp_path)))
    assert result.is_error is True
    assert "already in use" in result.output
    assert "mem_first" in result.output


def test_memory_save_persists_belief_fields(tmp_path: Path) -> None:
    """memory_save must propagate slug/confidence/expiry into the file."""
    store, index, fts, _pipeline = _bundle(tmp_path)
    tool = MemorySaveTool(store=store, index=index, fts_index=fts)
    inp = MemorySaveInput(
        type="project",
        title="TTS provider choice",
        content="Gemini Flash TTS with Charon timbre is the operator's seed image; TARS may propose drift via SOUL/IDENTITY.",
        slug="tts_provider",
        confidence=0.9,
        expiry_at="2030-01-01T00:00:00Z",
        entities=["tts", "charon"],
    )
    result = asyncio.run(tool.run(inp, _ctx(tmp_path)))
    assert result.is_error is not True
    assert "slug=tts_provider" in result.output
    saved = [m for m in store.list_all() if m.slug == "tts_provider"]
    assert len(saved) == 1
    fm = saved[0]
    assert fm.confidence == pytest.approx(0.9)
    assert fm.expiry_at == datetime(2030, 1, 1, tzinfo=timezone.utc)
    assert fm.entities == ["tts", "charon"]


def test_memory_save_rejects_bad_expiry(tmp_path: Path) -> None:
    store, index, fts, _pipeline = _bundle(tmp_path)
    tool = MemorySaveTool(store=store, index=index, fts_index=fts)
    inp = MemorySaveInput(
        type="project",
        title="Bad expiry test",
        content="A test memory with an intentionally malformed expiry_at string to confirm the save path validates input correctly.",
        expiry_at="not-a-date",
    )
    result = asyncio.run(tool.run(inp, _ctx(tmp_path)))
    assert result.is_error is True
    assert "Invalid expiry_at" in result.output


def test_memory_save_rejects_bad_slug_without_crashing(tmp_path: Path) -> None:
    store, index, fts, _pipeline = _bundle(tmp_path)
    tool = MemorySaveTool(store=store, index=index, fts_index=fts)
    inp = MemorySaveInput(
        type="project",
        title="Bad slug test",
        content="A test memory with an intentionally malformed slug to confirm the save path returns a tool error.",
        slug="Bad-Slug",
    )
    result = asyncio.run(tool.run(inp, _ctx(tmp_path)))
    assert result.is_error is True
    assert "Invalid slug" in result.output


def test_memory_search_output_shows_provenance_and_confidence(tmp_path: Path) -> None:
    """memory_search formatted output must include via= and confidence=."""
    store, index, fts, pipeline = _bundle(tmp_path)
    _seed(
        store, fts,
        mem_id="mem_proven",
        title="Provenance check",
        body="The permission engine has a 24-check deny list that fires before the policy lookup and cannot be overridden.",
        confidence=0.75,
    )
    tool = MemorySearchTool(pipeline=pipeline)
    inp = MemorySearchInput(query="permission engine deny")
    result = asyncio.run(tool.run(inp, _ctx(tmp_path)))
    assert result.is_error is not True
    out = result.output
    assert "via:" in out
    assert "confidence: 0.75" in out
    assert "bm25" in out


def test_memory_search_short_circuit_banner(tmp_path: Path) -> None:
    """When stage 0 short-circuits on slug, the output prefixes the banner."""
    store, index, fts, pipeline = _bundle(tmp_path)
    _seed(
        store, fts,
        mem_id="mem_slug_hit",
        title="Slug hit",
        body="A canonical decision body intentionally long enough to clear the trivial-body filter on writes to memory store.",
        slug="canonical_x",
    )
    tool = MemorySearchTool(pipeline=pipeline)
    result = asyncio.run(tool.run(MemorySearchInput(query="canonical_x"), _ctx(tmp_path)))
    assert "exact slug match" in result.output
    assert "exact_slug" in result.output


def test_memory_update_preserves_belief_fields(tmp_path: Path) -> None:
    """Reviewer finding #1 (2026-04-29) — memory_update used to silently drop
    slug/confidence/expiry_at on every edit. This regression locks the fix in."""
    from tesseract.kernel.tools.memory_update import MemoryUpdateInput, MemoryUpdateTool

    store, index, fts, _pipeline = _bundle(tmp_path)
    expiry = datetime(2030, 6, 1, tzinfo=timezone.utc)
    fm = _seed(
        store, fts,
        mem_id="mem_to_edit",
        title="Original title",
        body="Original body content with enough length to satisfy the trivial-body filter on every memory write.",
        slug="preserved_slug",
        confidence=0.65,
        expiry_at=expiry,
        entities=["x"],
    )
    tool = MemoryUpdateTool(store=store, index=index, fts_index=fts)
    inp = MemoryUpdateInput(
        memory_id=fm.id,
        title="Updated title",
        content="Updated body content with enough length to satisfy the trivial-body filter on every memory write.",
    )
    result = asyncio.run(tool.run(inp, _ctx(tmp_path)))
    assert result.is_error is not True

    after = store.read(fm.id)
    assert after is not None
    fm_after, _body = after
    assert fm_after.slug == "preserved_slug"
    assert fm_after.confidence == pytest.approx(0.65)
    assert fm_after.expiry_at == expiry
    assert fm_after.title == "Updated title"


def test_auto_linker_preserves_belief_fields(tmp_path: Path) -> None:
    """Reviewer finding #2 (2026-04-29) — auto_linker rebuilt MemoryFrontmatter
    by hand and dropped slug/confidence/expiry_at on every neighbor rewrite."""
    from tesseract.memory.auto_linker import AutoLinker

    store, _index, fts, _pipeline = _bundle(tmp_path)
    expiry = datetime(2030, 6, 1, tzinfo=timezone.utc)
    fm = _seed(
        store, fts,
        mem_id="mem_with_belief",
        title="A memory with belief fields",
        body="Body for the auto-linker preservation test, padded out so it clears the trivial-body floor easily.",
        slug="preserved_via_auto_link",
        confidence=0.55,
        expiry_at=expiry,
    )
    # Bypass the cosine search by calling _add_auto_links directly — the goal
    # is to confirm the rewrite path, not the embedding lookup.
    linker = AutoLinker.__new__(AutoLinker)
    linker._store = store
    linker._embeddings = None  # not used by _add_auto_links
    linker._add_auto_links(fm.id, ["mem_some_other"])

    after = store.read(fm.id)
    assert after is not None
    fm_after, _body = after
    assert fm_after.slug == "preserved_via_auto_link"
    assert fm_after.confidence == pytest.approx(0.55)
    assert fm_after.expiry_at == expiry
    assert "mem_some_other" in fm_after.auto_links


def test_invalid_slug_format_raises_at_construction() -> None:
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError):
        MemoryFrontmatter(
            id="mem_xyz",
            type=MemoryType.PROJECT,
            title="bad slug",
            created_at=now,
            slug="Has-Caps-Or-Dashes",
        )


def test_confidence_weights_stage_a_ranking(tmp_path: Path) -> None:
    """Two memories with identical importance/recency/overlap rank by
    confidence: the higher-confidence memory wins. Locks in the ranking
    weight added 2026-04-30 alongside the belief-state schema (Phase 1 d)."""
    store, _index, fts, pipeline = _bundle(tmp_path)
    _seed(
        store, fts,
        mem_id="mem_low_conf",
        title="Permission policy is lenient by default",
        body="Permission policy is lenient by default; tools run AUTO unless explicitly listed under ASK or DENY for safety reasons.",
        confidence=0.3,
    )
    _seed(
        store, fts,
        mem_id="mem_high_conf",
        title="Permission policy is strict by default",
        body="Permission policy is strict by default; every outbound or mutating call hits an explicit ASK gate before it executes.",
        confidence=0.95,
    )
    candidates = pipeline.stage_a_prefilter("permission policy default")
    ids = [fm.id for fm in candidates]
    assert "mem_high_conf" in ids
    assert "mem_low_conf" in ids
    assert ids.index("mem_high_conf") < ids.index("mem_low_conf"), (
        f"high-confidence memory should rank before low-confidence one; got {ids}"
    )


def test_confidence_weights_stage_b_final_ranking(tmp_path: Path) -> None:
    """End-to-end retrieval: confidence weighting must survive Stage A's
    candidate prefilter into Stage B's RRF score so the final order
    actually reflects belief strength, not just which entries qualified.
    Two memories with similar BM25 signals — the higher-confidence one
    must lead the returned packet."""
    store, _index, fts, pipeline = _bundle(tmp_path)
    _seed(
        store, fts,
        mem_id="mem_low_conf_b",
        title="Default voice profile is Charon timbre",
        body="Default voice profile is Charon timbre with conversational mode unless the operator explicitly opts into the alternate Australian-English voice profile.",
        confidence=0.2,
    )
    _seed(
        store, fts,
        mem_id="mem_high_conf_b",
        title="Default voice profile is Charon timbre",
        body="Default voice profile is Charon timbre with conversational mode unless the operator explicitly opts into the alternate male British voice profile.",
        confidence=0.95,
    )
    packet = asyncio.run(pipeline.retrieve("default voice profile charon", top_k=5))
    ids = [r.memory_id for r in packet.results]
    assert "mem_high_conf_b" in ids, f"high-confidence memory missing from packet: {ids}"
    assert "mem_low_conf_b" in ids, f"low-confidence memory missing from packet: {ids}"
    assert ids.index("mem_high_conf_b") < ids.index("mem_low_conf_b"), (
        f"high-confidence memory should rank before low-confidence one in final results; got {ids}"
    )
