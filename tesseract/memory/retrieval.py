"""Multi-stage retrieval pipeline (A + B-hybrid + C-post + D-parallel).

Stage A: metadata prefilter (tags, importance, recency, type).
Stage B: hybrid search — BM25 (FTS5) + Vector (FAISS) merged via RRF,
         with temporal decay.
Stage C-post: LLM evaluate/rerank via selector (optional).
Stage D: path expansion via neighbor traversal (parallel with B+C).
Degrades gracefully — FTS, C-post, and D are skipped when not configured
or on failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from tesseract.memory.embeddings import EmbeddingIndex
from tesseract.memory.fts_index import FTSIndex
from tesseract.memory.index import MemoryIndex
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType, RetrievalPacket
from tesseract.memory.work_index import WorkIndex
from tesseract.orchestrator.progress_events import ProgressEvent, emit as emit_progress

logger = logging.getLogger(__name__)

_TRUSTING_RECALL_SECTION = (
    "\n\n--- MEMORY RECALL NOTICE ---\n"
    "A memory naming a file, function, or flag is a claim it existed when written. "
    "It may have been renamed or removed. Before recommending: verify the file exists, "
    "grep for the function. 'Memory says X exists' ≠ 'X exists now.'\n"
    "--- END NOTICE ---"
)

# CR-1 M3 — trust text for work-history hits. These are session
# transcripts and workshop artifacts, NOT promoted memory. The model
# must treat them as recall (suggestions) rather than ground truth.
_WORK_HISTORY_NOTICE = (
    "\n\n--- WORK HISTORY NOTICE ---\n"
    "Work-history hits below are non-authoritative — session transcripts "
    "and workshop artifacts surfaced for recall, NOT promoted memory. "
    "Treat them as suggestions; verify against the source path before "
    "acting. `file_read` the path for full context.\n"
    "--- END NOTICE ---"
)

_RRF_K = 60
_TEMPORAL_HALF_LIFE_DAYS = 30
_TEMPORAL_HIGH_IMPORTANCE_HALF_LIFE_DAYS = 60
_HIGH_IMPORTANCE_THRESHOLD = 8
_OVERLAP_BOOST = 0.15

# Reranked scores live in (0, _RERANK_SCORE_CEIL] so exact_slug (1.0) and
# exact_entity (0.9) hits always outrank any cross-encoder opinion.
_RERANK_SCORE_CEIL = 0.85
# Public: callers (e.g. auto_recall) need this to over-fetch candidates for
# local dedup backfill, since retrieve()'s final cap is this constant, not
# the caller's top_k.
MAX_FINAL_RESULTS = 7
# Phrase-window cap for Stage 0 exact lookup. Covers the vast majority of
# person/entity names ("Ada Lovelace", "Dr. Ada Lovelace"); raise only if
# indexing long org/book titles. Cost is O(N * cap) phrases per query.
_STAGE_ZERO_PHRASE_MAX_WORDS = 4


def _normalize_lookup(text: str) -> str:
    """Casefold, strip non-alphanumerics to whitespace, collapse runs.

    Used for Stage 0 exact lookup so `Ada Lovelace`, `ada lovelace`,
    `who is Ada Lovelace?`, and `ADA-LOVELACE` all converge to the same
    canonical form. Strips underscores too — slug-format keys are rebuilt
    in `_query_phrases` by re-joining with `_`.
    """
    text = text.casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _query_phrases(
    query: str,
    max_words: int = _STAGE_ZERO_PHRASE_MAX_WORDS,
) -> tuple[set[str], set[str]]:
    """Generate phrase windows + slug variants for Stage 0 exact lookup.

    Returns `(phrases, slug_variants)`. Phrases are normalized 1..max_words
    word substrings; slug_variants are those phrases with spaces replaced
    by `_` so saved slugs (which use `[a-z0-9_]+`) can match a natural
    query that didn't use the underscore form.
    """
    norm = _normalize_lookup(query)
    if not norm:
        return set(), set()
    words = norm.split()
    phrases: set[str] = {norm}
    for i in range(len(words)):
        for j in range(i + 1, min(len(words), i + max_words) + 1):
            phrases.add(" ".join(words[i:j]))
    slug_variants = {p.replace(" ", "_") for p in phrases}
    return phrases, slug_variants


@dataclass(frozen=True)
class RetrievalResult:
    memory_id: str
    title: str
    body: str
    score: float
    mem_type: MemoryType
    # Provenance — which retrieval routes contributed to this hit. Set by the
    # pipeline; e.g. ("exact_slug",), ("bm25", "vector"), ("prefilter",),
    # ("neighbor",). Empty tuple is treated as "unknown route" by formatters.
    provenance: tuple[str, ...] = ()
    # Operator/save-time confidence carried over from the memory frontmatter
    # (1.0 when unset). Distinct from `score` — score is retrieval relevance,
    # confidence is how much the saver trusted the fact.
    confidence: float = 1.0
    # True when the conversation this memory was learned from has been deleted.
    # The fact still stands; there is simply no transcript left to go back to.
    source_deleted: bool = False


class RetrievalPipeline:
    def __init__(
        self,
        store: MemoryStore,
        index: MemoryIndex,
        embeddings: EmbeddingIndex | None = None,
        fts_index: FTSIndex | None = None,
        selector: object | None = None,
        selector_adapter: object | None = None,
        path_expander: object | None = None,
        recall_log_path: Path | None = None,
        progress_cfg: dict | None = None,
        work_index: WorkIndex | None = None,
        reranker: object | None = None,
    ) -> None:
        self._store = store
        self._index = index
        self._embeddings = embeddings
        self._fts_index = fts_index
        self._selector = selector
        self._selector_adapter = selector_adapter
        self._path_expander = path_expander
        self._recall_log_path = recall_log_path
        self._progress_cfg = progress_cfg or {}
        # Cross-encoder precision stage over the merged pool (role-wired,
        # best-effort). None → retrieval keeps pure RRF ordering.
        self._reranker = reranker
        # CR-1 M3 — non-authoritative work-history retrieval. When set,
        # `retrieve(..., include_work_history=True)` augments the memory
        # packet with `session:` / `workshop:` chunks. Promotion to
        # authoritative memory still requires the librarian path.
        self._work_index = work_index

    def load_hot_index(self) -> str:
        return self._index.load_raw()

    def load_daily_notes(self, days: int = 2) -> str:
        """Load recent daily notes (today + yesterday by default)."""
        notes = self._store.list_daily_notes()
        if not notes:
            return ""
        parts: list[str] = []
        for note_path in notes[:days]:
            content = note_path.read_text(encoding="utf-8")
            parts.append(content)
        return "\n\n".join(parts)

    def _log_recalls(self, query: str, results: list[RetrievalResult]) -> None:
        """Log recall events for each returned memory (feeds dreaming engine)."""
        if self._recall_log_path is None or not results:
            return
        try:
            self._recall_log_path.parent.mkdir(parents=True, exist_ok=True)
            now = datetime.now(timezone.utc).isoformat()
            with self._recall_log_path.open("a", encoding="utf-8") as f:
                for r in results:
                    entry = {
                        "memory_id": r.memory_id,
                        "query": query,
                        "confidence": r.score,
                        "timestamp": now,
                    }
                    f.write(json.dumps(entry) + "\n")
        except Exception:
            logger.warning("Failed to log recall events")

    def stage_zero_exact(
        self,
        query: str,
        type_filter: MemoryType | None = None,
        entries: list[MemoryFrontmatter] | None = None,
    ) -> tuple[list[RetrievalResult], bool]:
        """Exact match on slug or entity. Returns (hits, short_circuit).

        Slug match is the canonical decision lookup — when a saved memory has
        `slug: voice_default` and the query mentions `voice_default` (case-
        insensitive, exact token), the pipeline should return that memory and
        stop. Entity match is weaker — it promotes hits but still lets the
        rest of the pipeline run so semantic neighbors can join.

        `entries` lets the caller pass a pre-fetched frontmatter list so the
        full-store scan happens once per `retrieve()` (shared with stage A)
        instead of twice; falls back to `list_all()` for direct callers.
        Returns frontmatter list, not bodies, to avoid loading non-matches.
        """
        all_fms = entries if entries is not None else self._store.list_all(type_filter=type_filter)
        if not all_fms:
            return [], False

        now = datetime.now(timezone.utc)
        all_fms = [fm for fm in all_fms if fm.expiry_at is None or fm.expiry_at > now]

        # Phrase windows + slug variants — see _normalize_lookup / _query_phrases.
        # A query like "who is Ada Lovelace?" produces phrases that include
        # "ada lovelace", which then matches a stored entity "Ada Lovelace"
        # after both sides go through the same normalizer. Slug variants
        # rebuild underscore-joined keys so saved slugs still match.
        phrases, slug_variants = _query_phrases(query)

        # Group hits by the matched key so ambiguity means "same identity
        # claimed by ≥2 records," not "the query matched two unrelated
        # records." Slug ambiguity is normally blocked at save time but a
        # manual file edit could bypass it; tag it defensively.
        slug_hits_by_key: dict[str, list[MemoryFrontmatter]] = {}
        entity_hits_by_phrase: dict[str, list[MemoryFrontmatter]] = {}
        for fm in all_fms:
            if fm.slug:
                slug_lower = fm.slug.lower()
                if slug_lower in slug_variants:
                    slug_hits_by_key.setdefault(slug_lower, []).append(fm)
                    continue
            if fm.entities:
                normalized_entities = {_normalize_lookup(e) for e in fm.entities}
                normalized_entities.discard("")
                for matched in normalized_entities & phrases:
                    entity_hits_by_phrase.setdefault(matched, []).append(fm)

        ambiguous_slug_ids = {
            fm.id for records in slug_hits_by_key.values() if len(records) >= 2 for fm in records
        }
        ambiguous_entity_ids = {
            fm.id for records in entity_hits_by_phrase.values() if len(records) >= 2 for fm in records
        }
        # Flatten preserving first-seen order; an entity record matched via
        # multiple phrases only appears once in the result list.
        slug_hits: list[MemoryFrontmatter] = [fm for records in slug_hits_by_key.values() for fm in records]
        seen_entity_ids: set[str] = set()
        entity_hits: list[MemoryFrontmatter] = []
        for records in entity_hits_by_phrase.values():
            for fm in records:
                if fm.id not in seen_entity_ids:
                    seen_entity_ids.add(fm.id)
                    entity_hits.append(fm)

        results: list[RetrievalResult] = []
        for fm in slug_hits:
            read_result = self._store.read(fm.id, log_access=False)
            if read_result is None:
                continue
            _, body = read_result
            prov: tuple[str, ...] = ("exact_slug", "exact_ambiguous") if fm.id in ambiguous_slug_ids else ("exact_slug",)
            results.append(RetrievalResult(
                memory_id=fm.id,
                title=fm.title,
                body=body,
                score=1.0,
                mem_type=fm.type,
                provenance=prov,
                confidence=fm.confidence,
                source_deleted=fm.source_deleted_at is not None,
            ))
        for fm in entity_hits:
            read_result = self._store.read(fm.id, log_access=False)
            if read_result is None:
                continue
            _, body = read_result
            prov = ("exact_entity", "exact_ambiguous") if fm.id in ambiguous_entity_ids else ("exact_entity",)
            results.append(RetrievalResult(
                memory_id=fm.id,
                title=fm.title,
                body=body,
                score=0.9,
                mem_type=fm.type,
                provenance=prov,
                confidence=fm.confidence,
                source_deleted=fm.source_deleted_at is not None,
            ))

        # Short-circuit only on a single slug hit. Multiple slug hits
        # (manual-edit pathology — saves block dupes) and entity hits both
        # let B+D run so any disambiguating neighbors can join.
        short_circuit = len(slug_hits) == 1
        return results, short_circuit

    def stage_a_prefilter(
        self,
        query: str,
        type_filter: MemoryType | None = None,
        min_importance: int = 1,
        max_candidates: int = 30,
        entries: list[MemoryFrontmatter] | None = None,
    ) -> list[MemoryFrontmatter]:
        # `entries` shares stage 0's pre-fetched frontmatter list so the
        # full-store scan runs once per retrieve(); falls back to list_all().
        all_fms = entries if entries is not None else self._store.list_all(type_filter=type_filter)

        # Drop expired memories — soft delete from retrieval surface only.
        # File stays on disk; an operator can revive it by editing expiry_at.
        now = datetime.now(timezone.utc)
        all_fms = [fm for fm in all_fms if fm.expiry_at is None or fm.expiry_at > now]

        query_lower = query.lower()
        query_words = set(query_lower.split())

        scored: list[tuple[MemoryFrontmatter, float]] = []
        for fm in all_fms:
            if fm.importance < min_importance:
                continue

            score = fm.importance / 10.0

            tags_lower = {t.lower() for t in fm.tags}
            entities_lower = {e.lower() for e in fm.entities}
            title_words = set(fm.title.lower().split())
            summary_words = set(fm.summary.lower().split())

            tag_overlap = query_words & tags_lower
            entity_overlap = query_words & entities_lower
            title_overlap = query_words & title_words
            summary_overlap = query_words & summary_words

            score += len(tag_overlap) * 0.3
            score += len(entity_overlap) * 0.2
            score += len(title_overlap) * 0.2
            score += len(summary_overlap) * 0.1

            if fm.updated_at:
                age_days = (datetime.now(timezone.utc) - fm.updated_at).days
            else:
                age_days = (datetime.now(timezone.utc) - fm.created_at).days
            recency_boost = max(0, 1.0 - (age_days / 365.0))
            score += recency_boost * 0.2

            # Confidence acts as a final multiplier. Default 1.0 preserves
            # legacy behavior; lower values dampen ranking proportionally.
            score *= fm.confidence

            scored.append((fm, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [fm for fm, _ in scored[:max_candidates]]

    async def stage_b_hybrid_search(
        self,
        query: str,
        query_vector: np.ndarray | None,
        candidates: list[MemoryFrontmatter],
        top_k: int = 5,
        type_filter: MemoryType | None = None,
    ) -> list[RetrievalResult]:
        """BM25 + Vector search merged via RRF with temporal decay.

        RC1 fix (2026-07-08): BM25 and vector search the FULL index, not
        just Stage A's literal word-overlap candidates. Stage A's
        `title.lower().split()` scoring drops exact-substring queries whose
        token doesn't equal a title token verbatim (e.g. query "gate_fizz"
        vs title token "gate_fizz.py" — the ".py" suffix breaks set
        equality), which used to starve Stage B of a candidate that both
        FTS5 tokenization and vector cosine would otherwise find cleanly.
        `candidates` is no longer a gate on the search itself — it's merged
        into the scoring context below (decay/confidence/expiry/type) so
        every result, whether or not it made Stage A's top list, is
        weighted and filtered consistently. `type_filter` is re-checked
        here (not just at Stage A) for the same reason as expiry: the old
        `candidate_ids` gate used to make Stage A's own `type_filter`
        scoping implicitly apply to Stage B too — removing the gate means
        a full-index BM25/vector hit of a different `MemoryType` has to be
        dropped explicitly instead.
        """
        candidate_map = {fm.id: fm for fm in candidates}

        # BM25 search via FTS5 — full index, unfiltered (RC1).
        bm25_ranked: list[tuple[str, float]] = []
        if self._fts_index is not None:
            try:
                bm25_ranked = self._fts_index.search(
                    query, limit=top_k * 3, require_prefix="mem_"
                )
            except Exception:
                logger.warning("BM25 search failed, continuing with vector only")

        # Vector search via FAISS — full index, unfiltered (RC1). Gracefully
        # skipped when embeddings are unavailable (Ollama down at boot, or
        # `embeddings=None`). Audit M2 fix (2026-04-29): the live path used
        # to require embeddings; now BM25-only retrieval is a first-class
        # mode.
        vector_ranked: list[tuple[str, float]] = []
        if self._embeddings is not None:
            if query_vector is not None:
                try:
                    # Offload the synchronous FAISS search off the event loop
                    # (audit 2026-07-18, MED). Runs per turn; the index lock is
                    # a threading.Lock, so a worker thread is safe.
                    vector_ranked = await asyncio.to_thread(
                        lambda: self._embeddings.search_by_vector(
                            query_vector, top_k=top_k * 3, require_prefix="mem_"
                        )
                    )
                except Exception:
                    logger.warning("Vector search failed, continuing with BM25 only")
            else:
                try:
                    vector_ranked = await self._embeddings.search(
                        query, top_k=top_k * 3, require_prefix="mem_",
                    )
                except Exception:
                    logger.warning("Vector search failed, continuing with BM25 only")

        # RRF merge: 1/(K + rank). Provenance tracks which routes contributed
        # to each id so the surfaced hit explains why it matched (spec §2).
        # The indexes are SHARED with vault chunks (vault:*) and daily notes
        # (daily_*). Those rows are not memory results — letting them into
        # rrf_scores hands them top_k slots that get silently discarded at
        # the read step, squeezing real memories out of the packet. Memory
        # ids always start "mem_" (types.py validator).
        rrf_scores: dict[str, float] = {}
        provenance: dict[str, list[str]] = {}
        for rank, (mem_id, _) in enumerate(bm25_ranked):
            if not mem_id.startswith("mem_"):
                continue
            rrf_scores[mem_id] = rrf_scores.get(mem_id, 0) + 1 / (_RRF_K + rank)
            provenance.setdefault(mem_id, []).append("bm25")
        for rank, (mem_id, _) in enumerate(vector_ranked):
            if not mem_id.startswith("mem_"):
                continue
            rrf_scores[mem_id] = rrf_scores.get(mem_id, 0) + 1 / (_RRF_K + rank)
            provenance.setdefault(mem_id, []).append("vector")

        if not rrf_scores:
            return []

        # Temporal decay: 0.5^(days / half_life). RC1: full-index search
        # can surface ids outside Stage A's candidate pool — look those up
        # so decay/confidence/expiry still apply consistently, and drop any
        # hit that has since expired (Stage A's expiry filter no longer
        # gates these ids, so it has to be re-checked here).
        now = datetime.now(timezone.utc)
        for mem_id in list(rrf_scores.keys()):
            fm = candidate_map.get(mem_id)
            if fm is None:
                read_result = self._store.read(mem_id, log_access=False)
                if read_result is not None:
                    fm, _ = read_result
                    candidate_map[mem_id] = fm
            if fm is None:
                # Ghost id — indexed but no longer readable (deleted since).
                # It can never fill a result slot, so it must not hold one.
                del rrf_scores[mem_id]
                provenance.pop(mem_id, None)
                continue
            if fm.expiry_at is not None and fm.expiry_at <= now:
                del rrf_scores[mem_id]
                provenance.pop(mem_id, None)
                continue
            if type_filter is not None and fm.type != type_filter:
                del rrf_scores[mem_id]
                provenance.pop(mem_id, None)
                continue
            ref_date = fm.updated_at or fm.created_at
            age_days = max(0, (now - ref_date).days)
            half_life = (
                _TEMPORAL_HIGH_IMPORTANCE_HALF_LIFE_DAYS
                if fm.importance >= _HIGH_IMPORTANCE_THRESHOLD
                else _TEMPORAL_HALF_LIFE_DAYS
            )
            decay = math.pow(0.5, age_days / half_life)
            rrf_scores[mem_id] *= decay
            # Confidence weight on the merged RRF score so it survives into
            # the final ranking, not only the Stage A prefilter pool. Default
            # 1.0 preserves prior behavior; lower values dampen proportionally.
            rrf_scores[mem_id] *= fm.confidence

        ranked = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)[:top_k]

        results: list[RetrievalResult] = []
        for mem_id, score in ranked:
            read_result = self._store.read(mem_id, log_access=False)
            if read_result is None:
                continue
            fm, body = read_result
            results.append(RetrievalResult(
                memory_id=mem_id,
                title=fm.title,
                body=body,
                score=score,
                mem_type=fm.type,
                provenance=tuple(provenance.get(mem_id, ())),
                confidence=fm.confidence,
                source_deleted=fm.source_deleted_at is not None,
            ))
        return results

    async def stage_b_embedding_search(
        self,
        query: str,
        candidates: list[MemoryFrontmatter],
        top_k: int = 5,
    ) -> list[RetrievalResult]:
        """Legacy vector-only search. Kept for backward compatibility."""
        candidate_ids = [fm.id for fm in candidates]
        search_results = await self._embeddings.search(
            query, top_k=top_k, candidate_ids=candidate_ids,
        )

        results: list[RetrievalResult] = []
        for mem_id, score in search_results:
            read_result = self._store.read(mem_id, log_access=False)
            if read_result is None:
                continue
            fm, body = read_result
            # Confidence weight matches the hybrid-search path so the legacy
            # vector-only mode produces consistent ranking.
            weighted_score = score * fm.confidence
            results.append(RetrievalResult(
                memory_id=mem_id,
                title=fm.title,
                body=body,
                score=weighted_score,
                mem_type=fm.type,
                provenance=("vector",),
                confidence=fm.confidence,
                source_deleted=fm.source_deleted_at is not None,
            ))
        results.sort(key=lambda r: r.score, reverse=True)
        return results

    async def retrieve(
        self,
        query: str,
        type_filter: MemoryType | None = None,
        top_k: int = 5,
        *,
        include_work_history: bool = False,
        work_history_top_k: int = 5,
    ) -> RetrievalPacket:
        import uuid as _uuid
        _run_id = str(_uuid.uuid4())
        _pcfg = self._progress_cfg

        stages_run: list[str] = []

        # CR-1 M3 — fetch work-history once up front. None when the caller
        # didn't ask, when no work_index is wired, or on search failure.
        work_history = self._fetch_work_history(
            query,
            include=include_work_history,
            top_k=work_history_top_k,
        )

        # Stage 0 — exact match on slug or entity. Slug match short-circuits
        # the rest of the pipeline (canonical decision lookup); entity match
        # seeds the result list and lets B+D contribute neighbors.
        # Full-store frontmatter scan (`list_all` rglob+parse) is the hot cost
        # here — it ran TWICE per retrieve (stage 0 + stage A). Fetch it ONCE
        # and share it, offloaded off the event loop so the loop stays
        # responsive (audit 2026-07-18; ~500 files ≈ 1.7s locally). Read-only,
        # thread-safe.
        all_fms = await asyncio.to_thread(self._store.list_all, type_filter)
        zero_results, short_circuit = await asyncio.to_thread(
            self.stage_zero_exact, query, type_filter=type_filter, entries=all_fms
        )
        if zero_results:
            stages_run.append("0")
        if short_circuit:
            self._log_recalls(query, zero_results)
            emit_progress(ProgressEvent(
                run_id=_run_id,
                run_type="retrieval",
                step_index=4,
                step_total=4,
                label="Retrieval complete",
                status="done",
                detail=f"{len(zero_results)} result(s), short-circuit on exact_slug",
            ), _pcfg)
            return RetrievalPacket(
                results=zero_results[:MAX_FINAL_RESULTS],
                confidence=1.0,
                stages_run=tuple(stages_run),
                short_circuited=True,
                daily_notes=self.load_daily_notes(days=2),
                work_history=work_history,
            )

        emit_progress(ProgressEvent(
            run_id=_run_id,
            run_type="retrieval",
            step_index=0,
            step_total=4,
            label="Embedding query",
            status="started",
        ), _pcfg)

        # Embed query once — shared by B and D. Skipped entirely when
        # embeddings are unavailable; hybrid search then degrades to
        # BM25-only via stage_b_hybrid_search.
        query_vector: np.ndarray | None = None
        if self._embeddings is not None:
            try:
                raw = await self._embeddings.embed_text(query)
                if raw is not None:
                    query_vector = np.array([raw], dtype=np.float32)
                    import faiss
                    faiss.normalize_L2(query_vector)
                    query_vector = query_vector[0]
            except Exception:
                logger.warning("Query embedding failed, hybrid search degrades to BM25")
                emit_progress(ProgressEvent(
                    run_id=_run_id,
                    run_type="retrieval",
                    step_index=0,
                    step_total=4,
                    label="Embedding query",
                    status="failed",
                    detail="embed_text failed; degrading to BM25",
                ), _pcfg)

        # Stage A: metadata prefilter
        emit_progress(ProgressEvent(
            run_id=_run_id,
            run_type="retrieval",
            step_index=1,
            step_total=4,
            label="Stage A — metadata prefilter",
            status="in_progress",
        ), _pcfg)
        # Reuse stage 0's shared frontmatter list (no second full-store scan)
        # — pure CPU scoring, offloaded (audit 2026-07-18).
        candidates = await asyncio.to_thread(
            self.stage_a_prefilter, query, type_filter=type_filter, entries=all_fms
        )
        stages_run.append("A")

        if not candidates:
            emit_progress(ProgressEvent(
                run_id=_run_id,
                run_type="retrieval",
                step_index=4,
                step_total=4,
                label="Retrieval complete",
                status="done",
                detail="0 result(s) — no candidates after stage A",
            ), _pcfg)
            return RetrievalPacket(
                results=[],
                stages_run=tuple(stages_run),
                work_history=work_history,
            )

        # Launch D in parallel with B+C (uses top 3 A candidates as seeds)
        _D_SEED_COUNT = 3
        d_task: asyncio.Task | None = None
        d_contributions = 0
        if self._path_expander is not None:
            top_a_results = []
            for fm in candidates[:_D_SEED_COUNT]:
                read_result = self._store.read(fm.id, log_access=False)
                if read_result:
                    _, body = read_result
                    top_a_results.append(RetrievalResult(
                        memory_id=fm.id,
                        title=fm.title,
                        body=body,
                        score=fm.importance / 10.0,
                        mem_type=fm.type,
                        provenance=("prefilter",),
                        confidence=fm.confidence,
                    ))
            if top_a_results:
                d_task = asyncio.create_task(
                    self._path_expander.expand(
                        top_a_results, query, query_vector=query_vector,
                    )
                )

        # Stage B: hybrid search (BM25 + vector via RRF)
        emit_progress(ProgressEvent(
            run_id=_run_id,
            run_type="retrieval",
            step_index=2,
            step_total=4,
            label="Stage B — hybrid search (BM25 + vector)",
            status="in_progress",
        ), _pcfg)
        results = await self.stage_b_hybrid_search(
            query, query_vector, candidates, top_k=top_k, type_filter=type_filter,
        )
        stages_run.append("B")

        if not results:
            logger.info("Hybrid search returned nothing, falling back to Stage A ranking")
            results = []
            for fm in candidates[:top_k]:
                read_result = self._store.read(fm.id, log_access=False)
                if read_result:
                    _, body = read_result
                    results.append(RetrievalResult(
                        memory_id=fm.id,
                        title=fm.title,
                        body=body,
                        score=fm.importance / 10.0,
                        mem_type=fm.type,
                        provenance=("prefilter",),
                        confidence=fm.confidence,
                    ))

        # Stage C-post: LLM evaluate/rerank (if selector available)
        confidence = 0.0
        if self._selector is not None and self._selector_adapter is not None:
            try:
                selection = await self._selector.evaluate(
                    query=query,
                    candidates=results,
                    adapter=self._selector_adapter,
                )
                results = selection.selected
                confidence = selection.confidence
                stages_run.append("C-post")
            except Exception:
                logger.warning("C-post failed, using B results unfiltered")

        # Await D results
        neighbors: list[RetrievalResult] = []
        if d_task is not None:
            try:
                neighbors = await asyncio.wait_for(d_task, timeout=2.0)
                if neighbors:
                    d_contributions = len(neighbors)
                    stages_run.append("D")
            except asyncio.TimeoutError:
                logger.warning("D-stage timed out after 2s, continuing without neighbors")
            except Exception:
                logger.warning("Path expansion failed, continuing without neighbors")

        # Merge: C results + D neighbors, overlap boost, cap at MAX_FINAL_RESULTS.
        # Overlap also merges provenance — a hit found by both bm25 and neighbor
        # walking should report both routes.
        if neighbors:
            result_ids = {r.memory_id for r in results}
            for nbr in neighbors:
                if nbr.memory_id in result_ids:
                    results = [
                        RetrievalResult(
                            memory_id=r.memory_id,
                            title=r.title,
                            body=r.body,
                            score=r.score + _OVERLAP_BOOST,
                            mem_type=r.mem_type,
                            provenance=tuple(dict.fromkeys((*r.provenance, "neighbor"))),
                            confidence=r.confidence,
                        ) if r.memory_id == nbr.memory_id else r
                        for r in results
                    ]
                else:
                    results.append(nbr)

        # Splice in stage-0 entity hits (no slug short-circuit). Their high
        # base score (0.9) keeps them at or near the top after sort, and any
        # overlap with B/D results gets its provenance merged.
        if zero_results:
            result_ids = {r.memory_id for r in results}
            for zr in zero_results:
                if zr.memory_id in result_ids:
                    results = [
                        RetrievalResult(
                            memory_id=r.memory_id,
                            title=r.title,
                            body=r.body,
                            score=max(r.score, zr.score),
                            mem_type=r.mem_type,
                            provenance=tuple(dict.fromkeys((*zr.provenance, *r.provenance))),
                            confidence=r.confidence,
                        ) if r.memory_id == zr.memory_id else r
                        for r in results
                    ]
                else:
                    results.append(zr)

        # Stage R: cross-encoder precision pass over the merged pool.
        results = await self._apply_reranker(query, results)
        if any("reranked" in r.provenance for r in results):
            stages_run.append("R")

        results.sort(key=lambda r: r.score, reverse=True)
        results = results[:MAX_FINAL_RESULTS]

        # Log recall events for dreaming
        self._log_recalls(query, results)

        emit_progress(ProgressEvent(
            run_id=_run_id,
            run_type="retrieval",
            step_index=3,
            step_total=4,
            label="Retrieval complete",
            status="done",
            detail=f"{len(results)} result(s), stages={'+'.join(stages_run)}",
        ), _pcfg)

        # Load daily notes
        daily_notes = self.load_daily_notes(days=2)

        return RetrievalPacket(
            results=results,
            confidence=confidence,
            stages_run=tuple(stages_run),
            d_contributions=d_contributions,
            daily_notes=daily_notes,
            work_history=work_history,
        )

    async def _apply_reranker(
        self, query: str, results: list[RetrievalResult]
    ) -> list[RetrievalResult]:
        """Re-score the merged pool with the cross-encoder, if one is wired.

        Exact_* hits are never rescored — a slug/entity match outranks any
        model opinion — and reranked scores are capped at _RERANK_SCORE_CEIL
        (then confidence-weighted, matching the other stages). Any failure
        returns the input untouched.
        """
        if self._reranker is None or not results:
            return results
        pool = [
            r for r in results
            if not any(p.startswith("exact_") for p in r.provenance)
        ]
        if not pool:
            return results
        try:
            scored = await self._reranker.rerank(
                query, [(r.memory_id, f"{r.title}\n{r.body}") for r in pool]
            )
        except Exception:
            logger.warning("reranker failed — keeping RRF order", exc_info=True)
            return results
        if not scored:
            return results
        score_map = dict(scored)
        out: list[RetrievalResult] = []
        for r in results:
            cross = score_map.get(r.memory_id)
            if cross is None:
                out.append(r)
                continue
            out.append(RetrievalResult(
                memory_id=r.memory_id,
                title=r.title,
                body=r.body,
                score=_RERANK_SCORE_CEIL * cross * r.confidence,
                mem_type=r.mem_type,
                provenance=tuple(dict.fromkeys((*r.provenance, "reranked"))),
                confidence=r.confidence,
            ))
        out.sort(key=lambda r: r.score, reverse=True)
        return out

    def _fetch_work_history(
        self,
        query: str,
        *,
        include: bool,
        top_k: int,
    ) -> list:
        """Return work-history hits for ``query`` or ``[]`` on miss.

        Returns an empty list when ``include`` is False, when no
        ``work_index`` is wired, or on any search failure (the
        underlying ``WorkIndex.search`` already swallows DB errors).
        """
        if not include or self._work_index is None:
            return []
        try:
            return self._work_index.search(query, top_k=top_k)
        except Exception:  # noqa: BLE001
            logger.warning("retrieve: work_index search raised; skipping", exc_info=True)
            return []

    def format_for_context(self, results: list[RetrievalResult] | RetrievalPacket) -> str:
        work_history: list = []
        if isinstance(results, RetrievalPacket):
            work_history = list(getattr(results, "work_history", []) or [])
            if results.synthesis:
                parts = ["--- RETRIEVED MEMORIES (synthesized) ---"]
                parts.append(f"\n{results.synthesis}")
                parts.append(_TRUSTING_RECALL_SECTION)
                # Work-history still appended even when the memory pass
                # synthesized — different trust class, different block.
                wh_section = _format_work_history(work_history)
                if wh_section:
                    parts.append(wh_section)
                return "\n".join(parts)
            items = results.results
        else:
            items = results

        memory_items = [r for r in items if not r.memory_id.startswith("vault:")]
        vault_items = [r for r in items if r.memory_id.startswith("vault:")]

        if not items and not work_history:
            return ""

        parts: list[str] = []

        if memory_items:
            parts.append("--- RETRIEVED MEMORIES ---")
            for r in memory_items:
                via = "+".join(r.provenance) if r.provenance else "unknown"
                meta = f"via={via} score={r.score:.2f} confidence={r.confidence:.2f}"
                parts.append(f"\n### [{r.mem_type.value}] {r.title}  ({meta})\n{r.body}")
            parts.append(_TRUSTING_RECALL_SECTION)

        if vault_items:
            parts.append("\n--- SOURCE MATERIAL ---")
            for r in vault_items:
                source_path = r.memory_id.removeprefix("vault:")
                if ":chunk_" in source_path:
                    source_path = source_path.rsplit(":chunk_", 1)[0]
                parts.append(f"\n### [vault] {source_path}\n{r.body}")

        wh_section = _format_work_history(work_history)
        if wh_section:
            parts.append(wh_section)

        return "\n".join(parts)


def _format_work_history(hits: list) -> str:
    """Render the work-history block. Empty string when no hits."""
    if not hits:
        return ""
    parts: list[str] = ["\n--- WORK HISTORY (non-authoritative recall) ---"]
    for h in hits:
        try:
            label = f"{h.source}:{h.source_ref}"
            location = h.source_path
            if h.turn_idx is not None:
                location = f"{location} (turn {h.turn_idx})"
            ts_tag = f" @ {h.ts}" if h.ts else ""
            preview = (h.text or "").strip()
            if len(preview) > 480:
                preview = preview[:480] + "…"
            parts.append(f"\n### [{label}]{ts_tag}  `{location}`\n{preview}")
        except Exception:  # noqa: BLE001
            continue
    parts.append(_WORK_HISTORY_NOTICE)
    return "\n".join(parts)
