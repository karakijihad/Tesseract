"""Embeddings + FAISS semantic index.

The provider, embedding model, vector dimensions, and HTTP timeouts are all
injected from `tesseract/config/roles.yaml` (top-level `embeddings:` block;
the catalog entry it references lives in `providers.yaml`).
This module carries no defaults — swap providers by editing config only.
FAISS IndexFlatIP for cosine similarity on L2-normalized vectors.
Derived artifact — delete and rebuild from canonical .md files.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from pathlib import Path

import faiss
import httpx
import numpy as np

from tesseract import http_client

logger = logging.getLogger(__name__)

# Circuit breaker for `rebuild_from_store`. When the embedding backend is
# unreachable (Ollama down, or the pinned model evicted under VRAM pressure
# on a small GPU), every `embed_text` returns None. Without a breaker the
# rebuild loops every chunk doing a failing HTTP round-trip on the event loop
# (multi-second lag on a large store) and then swaps in an empty index. Abort
# after this many consecutive failures while nothing has embedded yet.
REBUILD_MAX_CONSECUTIVE_FAILURES = 3

# Cross-process staleness (Deferred 2026-07-12): a second process (controller
# CLI) holds its own EmbeddingIndex while the Mirror process rebuilds
# index.faiss on disk. Search paths stat the file and reload when it changed,
# throttled to one stat per interval. Code-level bound, not a config key
# (SKILL_MD_MAX_BYTES idiom).
RELOAD_CHECK_MIN_INTERVAL_S = 2.0

# In-flight embed HTTP calls per rebuild batch. Pipelines request latency
# without flooding Ollama (which serializes GPU work server-side anyway).
# Code-level bound, not a config key (SKILL_MD_MAX_BYTES idiom).
_REBUILD_EMBED_CONCURRENCY = 4


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class EmbeddingIndex:
    def __init__(
        self,
        *,
        derived_dir: Path,
        provider: str,
        base_url: str,
        model: str,
        dimensions: int,
        timeout_seconds: float,
        max_retries: int,
    ) -> None:
        self._derived_dir = derived_dir
        self._provider = provider
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._dimensions = dimensions
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._index_path = derived_dir / "index.faiss"
        self._map_path = derived_dir / "id_map.json"
        self._id_to_pos: dict[str, int] = {}
        self._pos_to_id: dict[int, str] = {}
        # Content hash per indexed id — lets rebuild_from_store reuse the
        # live vector for unchanged text instead of re-embedding everything.
        self._text_hashes: dict[str, str] = {}
        self._index: faiss.IndexFlatIP = faiss.IndexFlatIP(self._dimensions)
        # WP-1 blocker §B fix: serialize FAISS in-memory state + id_map
        # mutations across concurrent async tasks (chat turn vs synthetic
        # workspace turn both calling memory_save). threading.Lock works for
        # both sync and async callers; held only across FAISS C-calls + small
        # file writes (microseconds-to-tens-of-ms typical).
        self._lock = threading.Lock()
        # One shared client for all embed calls. Constructing httpx.AsyncClient
        # per call runs ssl.create_default_context() synchronously ON the event
        # loop (Windows cert-store enumeration, ~100ms+); at 2026-07-03 boot a
        # 2.2k-memory rebuild_from_store pinned the loop for minutes and the
        # supervisor heartbeat-killed the backend in a respawn loop.
        self._client: httpx.AsyncClient | None = None
        self._loaded_mtime_ns = 0
        self._next_reload_check = 0.0
        self._load()

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = http_client.async_client(timeout=self._timeout_seconds)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    def _load(self) -> None:
        if self._index_path.exists() and self._map_path.exists():
            try:
                self._index = faiss.read_index(str(self._index_path))
                self._load_id_map()
                logger.info("Loaded FAISS index with %d vectors", self._index.ntotal)
            except Exception:
                logger.warning("Failed to load FAISS index, starting fresh")
                self._index = faiss.IndexFlatIP(self._dimensions)
        self._loaded_mtime_ns = self._stat_mtime_ns()

    def _stat_mtime_ns(self) -> int:
        try:
            return max(
                self._index_path.stat().st_mtime_ns,
                self._map_path.stat().st_mtime_ns,
            )
        except OSError:
            return 0

    def _maybe_reload_from_disk(self) -> None:
        """Cross-process freshness: swap in the on-disk index when another
        process rewrote it since our load. Read failures (e.g. mid-write
        torn file) keep the CURRENT index — never degrade to empty; the
        next throttled check retries."""
        now = time.monotonic()
        if now < self._next_reload_check:
            return
        self._next_reload_check = now + RELOAD_CHECK_MIN_INTERVAL_S
        disk_mtime_ns = self._stat_mtime_ns()
        if disk_mtime_ns <= self._loaded_mtime_ns:
            return
        try:
            new_index = faiss.read_index(str(self._index_path))
            data = json.loads(self._map_path.read_text(encoding="utf-8"))
        except Exception:
            logger.debug(
                "cross-process index reload skipped (unreadable, likely mid-write)",
                exc_info=True,
            )
            return
        with self._lock:
            self._index = new_index
            self._id_to_pos = data.get("id_to_pos", {})
            self._pos_to_id = {
                int(k): v for k, v in data.get("pos_to_id", {}).items()
            }
            self._text_hashes = data.get("text_hashes", {})
            self._loaded_mtime_ns = disk_mtime_ns
        logger.info(
            "Reloaded FAISS index from disk (cross-process change, %d vectors)",
            new_index.ntotal,
        )

    def _load_id_map(self) -> None:
        if self._map_path.exists():
            data = json.loads(self._map_path.read_text(encoding="utf-8"))
            self._id_to_pos = data.get("id_to_pos", {})
            self._pos_to_id = {int(k): v for k, v in data.get("pos_to_id", {}).items()}
            self._text_hashes = data.get("text_hashes", {})

    def _save(self) -> None:
        self._derived_dir.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self._index, str(self._index_path))
        self._save_id_map()
        # Our own write must not look like a cross-process change.
        self._loaded_mtime_ns = self._stat_mtime_ns()

    def _save_id_map(self) -> None:
        self._derived_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "id_to_pos": self._id_to_pos,
            "pos_to_id": {str(k): v for k, v in self._pos_to_id.items()},
            "text_hashes": self._text_hashes,
        }
        self._map_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    async def warm_up(self) -> None:
        """Pre-warm the Ollama embedding model to avoid first-turn latency."""
        await self.embed_text("warmup")

    async def embed_text(self, text: str) -> list[float] | None:
        try:
            resp = await self._http_client().post(
                f"{self._base_url}/api/embeddings",
                # keep_alive=-1 pins the embedding model in Ollama's VRAM
                # for the lifetime of the Ollama process. Default is 5min,
                # which causes reload cost on every sparse retrieval cycle.
                json={"model": self._model, "prompt": text, "keep_alive": -1},
            )
            resp.raise_for_status()
            return resp.json()["embedding"]
        except Exception:
            logger.warning("Embedding failed (%s may be down)", self._provider)
            return None

    async def add(self, memory_id: str, text: str) -> bool:
        vec = await self.embed_text(text)
        if vec is None:
            return False

        arr = np.array([vec], dtype=np.float32)
        faiss.normalize_L2(arr)

        with self._lock:
            if memory_id in self._id_to_pos:
                self._remove_locked(memory_id)

            pos = self._index.ntotal
            try:
                self._index.add(arr)
            except Exception as exc:
                logger.error("FAISS index add failed for %s: %s", memory_id, exc)
                raise
            self._id_to_pos[memory_id] = pos
            self._pos_to_id[pos] = memory_id
            self._text_hashes[memory_id] = _text_hash(text)
            self._save()
            # Snapshot the compaction-trigger inputs inside the lock so a
            # concurrent remove() between unlock and check can't make the
            # numerator/denominator inconsistent (audit-fix M4).
            ntotal_snap = self._index.ntotal
            live_count = len(self._id_to_pos)

        if ntotal_snap > 20 and ntotal_snap > 0 and (1.0 - live_count / ntotal_snap) > 0.3:
            await self.compact()

        return True

    def _remove_locked(self, memory_id: str) -> None:
        """Internal remove — caller must hold `self._lock`."""
        if memory_id in self._id_to_pos:
            del self._pos_to_id[self._id_to_pos[memory_id]]
            del self._id_to_pos[memory_id]
            self._text_hashes.pop(memory_id, None)
            self._save_id_map()
            # id_map.json changed on disk — refresh the marker so our own
            # remove doesn't read as a cross-process change (review finding
            # 2026-07-13; same pattern as _save).
            self._loaded_mtime_ns = self._stat_mtime_ns()

    def remove(self, memory_id: str) -> None:
        with self._lock:
            self._remove_locked(memory_id)

    @property
    def fragmentation(self) -> float:
        """Ratio of orphaned vectors to total vectors. 0.0 = clean, 1.0 = all orphaned.

        Codex-fix M5 (2026-05-23): the numerator (``len(_id_to_pos)``) and
        denominator (``_index.ntotal``) live on two separate objects that a
        concurrent ``add`` / ``remove`` / ``rebuild`` mutates independently.
        Without the lock, ``consistency.py`` can read a torn pair and
        decide compaction is required when the index is actually clean
        (or vice-versa). Acquired briefly — both reads are O(1).
        """
        with self._lock:
            total = self._index.ntotal
            if total == 0:
                return 0.0
            live = len(self._id_to_pos)
            return 1.0 - (live / total)

    async def compact(self, memories: list[tuple[str, str]] | None = None) -> int:
        """Rebuild index from only live vectors. If memories provided, rebuild from source."""
        if memories is not None:
            return await self.rebuild(memories)

        with self._lock:
            live_ids = list(self._id_to_pos.keys())
            live_positions = [(mid, self._id_to_pos[mid]) for mid in live_ids]

            if not live_positions:
                self._index = faiss.IndexFlatIP(self._dimensions)
                self._id_to_pos.clear()
                self._pos_to_id.clear()
                self._save()
                return 0

            vectors = np.zeros((len(live_positions), self._dimensions), dtype=np.float32)
            for i, (_mid, pos) in enumerate(live_positions):
                vectors[i] = self._index.reconstruct(pos)

            self._index = faiss.IndexFlatIP(self._dimensions)
            self._id_to_pos.clear()
            self._pos_to_id.clear()

            self._index.add(vectors)
            for i, (mid, _pos) in enumerate(live_positions):
                self._id_to_pos[mid] = i
                self._pos_to_id[i] = mid
            live = set(self._id_to_pos)
            self._text_hashes = {
                k: v for k, v in self._text_hashes.items() if k in live
            }

            self._save()
            logger.info("Compacted FAISS index: %d live vectors", len(live_positions))
            return len(live_positions)

    def snapshot_ids(self) -> list[str]:
        """Return a lock-safe snapshot of the live memory ids.

        Audit-fix M5: callers (`vault_indexer.py`, `consistency.py`) used
        to reach into the private ``_id_to_pos.keys()`` outside the lock,
        racing against concurrent ``rebuild()`` / ``compact()`` that
        swaps the dict reference. This public method takes the lock,
        copies the keys, and returns — the caller iterates a frozen list
        and any further mutations land cleanly.
        """
        with self._lock:
            return list(self._id_to_pos.keys())

    def get_vector(self, memory_id: str) -> np.ndarray | None:
        """Return the stored L2-normalized vector for a memory, or None."""
        with self._lock:
            pos = self._id_to_pos.get(memory_id)
            if pos is None:
                return None
            try:
                return self._index.reconstruct(pos)
            except Exception:
                return None

    def search_by_vector(
        self,
        vector: np.ndarray,
        top_k: int = 5,
        candidate_ids: list[str] | None = None,
        require_prefix: str | None = None,
    ) -> list[tuple[str, float]]:
        """Search using a pre-embedded, L2-normalized vector.

        `require_prefix` keeps only matching ids. The index is shared
        between memory records and vault chunks; when a prefix is set the
        search window expands (bounded by ntotal) until top_k matching
        hits are found, so foreign vectors cannot crowd the window."""
        self._maybe_reload_from_disk()
        with self._lock:
            if self._index.ntotal == 0:
                return []

            arr = vector.reshape(1, -1).astype(np.float32)
            search_k = min(top_k * 3, self._index.ntotal)
            while True:
                scores, indices = self._index.search(arr, search_k)

                results: list[tuple[str, float]] = []
                for score, idx in zip(scores[0], indices[0]):
                    if idx == -1:
                        continue
                    mem_id = self._pos_to_id.get(int(idx))
                    if mem_id is None:
                        continue
                    if require_prefix and not mem_id.startswith(require_prefix):
                        continue
                    if candidate_ids is not None and mem_id not in candidate_ids:
                        continue
                    results.append((mem_id, float(score)))

                if (
                    (not require_prefix and candidate_ids is None)
                    or len(results) >= top_k
                    or search_k >= self._index.ntotal
                ):
                    break
                search_k = min(search_k * 4, self._index.ntotal)

            results.sort(key=lambda x: x[1], reverse=True)
            return results[:top_k]

    async def search(
        self,
        query: str,
        top_k: int = 5,
        candidate_ids: list[str] | None = None,
        require_prefix: str | None = None,
    ) -> list[tuple[str, float]]:
        self._maybe_reload_from_disk()
        if self._index.ntotal == 0:
            return []

        vec = await self.embed_text(query)
        if vec is None:
            return []

        arr = np.array([vec], dtype=np.float32)
        faiss.normalize_L2(arr)
        return self.search_by_vector(
            arr[0],
            top_k=top_k,
            candidate_ids=candidate_ids,
            require_prefix=require_prefix,
        )

    async def rebuild(self, memories: list[tuple[str, str]]) -> int:
        """Atomic rebuild — builds a fresh index into local state, then swaps.

        Concurrent readers see the OLD index throughout the rebuild and the
        NEW index after the swap. Never empty mid-rebuild.
        """
        new_index = faiss.IndexFlatIP(self._dimensions)
        new_id_to_pos: dict[str, int] = {}
        new_pos_to_id: dict[int, str] = {}

        new_hashes: dict[str, str] = {}
        count = 0
        for memory_id, text in memories:
            vec = await self.embed_text(text)
            if vec is None:
                continue
            arr = np.array([vec], dtype=np.float32)
            faiss.normalize_L2(arr)
            pos = new_index.ntotal
            new_index.add(arr)
            new_id_to_pos[memory_id] = pos
            new_pos_to_id[pos] = memory_id
            new_hashes[memory_id] = _text_hash(text)
            count += 1

        with self._lock:
            self._index = new_index
            self._id_to_pos = new_id_to_pos
            self._pos_to_id = new_pos_to_id
            self._text_hashes = new_hashes
            self._save()
        logger.info("Rebuilt FAISS index with %d vectors", count)
        return count

    async def rebuild_from_store(
        self, chunks: list, preserve_prefixes: tuple[str, ...] = ()
    ) -> int:
        """Atomically rebuild the FAISS index from a list of (id, text) chunk pairs.

        Accepts any iterable of objects with .memory_id and .body attributes,
        or plain (str, str) tuples. Replaces self._index atomically on success.

        The index is shared with vectors the caller cannot re-feed (vault
        chunks); ids matching `preserve_prefixes` are carried over from the
        live index verbatim — no re-embedding — before the swap.

        # Periodic rebuild hook: register this as a nightly maintenance task
        # in DreamingScheduler or HeartbeatScheduler to clear orphaned vectors.
        """
        bus = None
        try:
            from tesseract.orchestrator.background_event_bus import get_background_bus
            bus = get_background_bus()
            bus.publish("faiss_rebuild_started", {})
        except Exception:
            bus = None

        new_index = faiss.IndexFlatIP(self._dimensions)
        new_id_to_pos: dict[str, int] = {}
        new_pos_to_id: dict[int, str] = {}
        new_hashes: dict[str, str] = {}

        def _add_vector(memory_id: str, h: str, arr: np.ndarray) -> None:
            pos = new_index.ntotal
            new_index.add(arr)
            new_id_to_pos[memory_id] = pos
            new_pos_to_id[pos] = memory_id
            new_hashes[memory_id] = h

        normalized: list[tuple[str, str, str]] = []
        for chunk in chunks:
            if isinstance(chunk, tuple):
                memory_id, text = chunk
            else:
                memory_id = chunk.memory_id
                text = chunk.body
            normalized.append((memory_id, _text_hash(text), text))

        # Reuse pass — unchanged text keeps its live vector; only changed or
        # new chunks pay an embed round-trip. FAISS reconstructs run off the
        # loop so a large store can't stall health/WS/heartbeats.
        to_embed: list[tuple[str, str, str]] = []
        reused = 0

        def _reuse_pass() -> None:
            nonlocal reused
            with self._lock:
                for memory_id, h, text in normalized:
                    pos = self._id_to_pos.get(memory_id)
                    if pos is not None and self._text_hashes.get(memory_id) == h:
                        try:
                            vec = self._index.reconstruct(pos)
                        except Exception:
                            to_embed.append((memory_id, h, text))
                            continue
                        _add_vector(
                            memory_id, h, vec.reshape(1, -1).astype(np.float32)
                        )
                        reused += 1
                    else:
                        to_embed.append((memory_id, h, text))

        await asyncio.to_thread(_reuse_pass)

        count = reused
        embedded_ok = 0
        failures = 0
        consecutive_failures = 0
        try:
            for start in range(0, len(to_embed), _REBUILD_EMBED_CONCURRENCY):
                batch = to_embed[start:start + _REBUILD_EMBED_CONCURRENCY]
                vecs = await asyncio.gather(
                    *(self.embed_text(text) for _mid, _h, text in batch),
                    return_exceptions=True,
                )
                for (memory_id, h, _text), vec in zip(batch, vecs):
                    if isinstance(vec, BaseException) or vec is None:
                        failures += 1
                        consecutive_failures += 1
                        # Breaker: while nothing has embedded yet, a run of
                        # failures means the backend is down/evicted — abort
                        # rather than grind every remaining chunk. The live
                        # index stays untouched (new_index is discarded), so
                        # unchanged vectors lose nothing.
                        if (
                            embedded_ok == 0
                            and consecutive_failures >= REBUILD_MAX_CONSECUTIVE_FAILURES
                        ):
                            logger.warning(
                                "rebuild_from_store: embedding backend unavailable "
                                "(%d consecutive failures) — aborting, index left intact",
                                consecutive_failures,
                            )
                            if bus is not None:
                                bus.publish(
                                    "faiss_rebuild_finished",
                                    {"status": "aborted", "vector_count": 0},
                                )
                            return 0
                        # Stale fallback: a changed chunk whose re-embed failed
                        # keeps its previous vector — stale beats absent. The
                        # old hash is kept so the next rebuild retries the embed.
                        stale = self.get_vector(memory_id)
                        if stale is not None:
                            _add_vector(
                                memory_id,
                                self._text_hashes.get(memory_id, ""),
                                stale.reshape(1, -1).astype(np.float32),
                            )
                            count += 1
                        continue
                    consecutive_failures = 0
                    arr = np.array([vec], dtype=np.float32)
                    faiss.normalize_L2(arr)
                    _add_vector(memory_id, h, arr)
                    embedded_ok += 1
                    count += 1

            # Never swap an empty index over live vectors: if every embed
            # failed (breaker didn't trip because failures interleaved with a
            # non-empty store), keep the existing index rather than wiping it.
            if count == 0 and failures > 0:
                logger.warning(
                    "rebuild_from_store: all %d embed(s) failed — index left intact",
                    failures,
                )
                if bus is not None:
                    bus.publish(
                        "faiss_rebuild_finished",
                        {"status": "aborted", "vector_count": 0},
                    )
                return 0

            def _swap() -> int:
                with self._lock:
                    preserved = 0
                    if preserve_prefixes:
                        for mid, pos in self._id_to_pos.items():
                            if mid in new_id_to_pos or not mid.startswith(preserve_prefixes):
                                continue
                            try:
                                vec = self._index.reconstruct(pos)
                            except Exception:
                                continue
                            _add_vector(
                                mid,
                                self._text_hashes.get(mid, ""),
                                vec.reshape(1, -1).astype(np.float32),
                            )
                            preserved += 1
                    self._index = new_index
                    self._id_to_pos = new_id_to_pos
                    self._pos_to_id = new_pos_to_id
                    self._text_hashes = new_hashes
                    self._save()
                    return preserved

            preserved = await asyncio.to_thread(_swap)
            logger.info(
                "rebuild_from_store: %d vectors indexed (%d reused, %d embedded), "
                "%d preserved",
                count, reused, embedded_ok, preserved,
            )
            if bus is not None:
                bus.publish(
                    "faiss_rebuild_finished",
                    {"status": "ok", "vector_count": count},
                )
            return count
        except Exception as exc:
            if bus is not None:
                bus.publish(
                    "faiss_rebuild_finished",
                    {"status": "error", "vector_count": count, "error": str(exc)},
                )
            raise
