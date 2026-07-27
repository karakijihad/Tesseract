"""Dreaming engine — nightly memory consolidation.

Reads recall_log.jsonl, scores candidates using 4-factor formula,
promotes qualifying memories to MEMORY.md, and prunes stale daily notes.
"""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from tesseract.memory.index import MemoryIndex
from tesseract.memory.store import MemoryStore, extract_wikilinks
from tesseract.memory.types import MemoryFrontmatter

# Frontmatter marker formerly written by the mission engine's reflector
# (mission engine deleted — prune wave 1). Historical records only: any
# memory files already on disk with this source_type still promote
# correctly; nothing produces new ones.
_MISSION_REFLECTION_SOURCE_TYPE = "mission_reflection"

if TYPE_CHECKING:
    from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter

logger = logging.getLogger(__name__)

_FREQUENCY_WEIGHT = 0.35
_RELEVANCE_WEIGHT = 0.35
_DIVERSITY_WEIGHT = 0.15
_RECENCY_WEIGHT = 0.15
_RECENCY_HALF_LIFE_DAYS = 14.0

_MIN_SCORE = 0.75
_MIN_RECALL_COUNT = 3
_MIN_UNIQUE_QUERIES = 2

_MAX_DAILY_NOTE_AGE_DAYS = 30
_MAX_RECALL_LOG_AGE_DAYS = 30
_MAX_RECALL_LOG_LINES = 50_000


class RecallCandidate:
    __slots__ = ("memory_id", "recall_count", "avg_confidence", "unique_queries", "last_recalled")

    def __init__(
        self,
        memory_id: str,
        recall_count: int,
        avg_confidence: float,
        unique_queries: int,
        last_recalled: datetime,
    ) -> None:
        self.memory_id = memory_id
        self.recall_count = recall_count
        self.avg_confidence = avg_confidence
        self.unique_queries = unique_queries
        self.last_recalled = last_recalled


class DreamingEngine:
    def __init__(
        self,
        store: MemoryStore,
        index: MemoryIndex,
        recall_log_path: Path,
    ) -> None:
        self._store = store
        self._index = index
        self._recall_log_path = recall_log_path

    def log_recall(
        self,
        memory_id: str,
        query: str,
        confidence: float,
        timestamp: datetime | None = None,
    ) -> None:
        ts = timestamp or datetime.now(timezone.utc)
        entry = {
            "memory_id": memory_id,
            "query": query,
            "confidence": confidence,
            "timestamp": ts.isoformat(),
        }
        self._recall_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._recall_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def build_candidates(self, now: datetime | None = None) -> list[RecallCandidate]:
        now = now or datetime.now(timezone.utc)
        if not self._recall_log_path.exists():
            return []

        per_memory: dict[str, list[dict]] = defaultdict(list)
        with self._recall_log_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    mem_id = entry.get("memory_id")
                    if mem_id:
                        per_memory[mem_id].append(entry)
                except json.JSONDecodeError:
                    continue

        candidates: list[RecallCandidate] = []
        for mem_id, entries in per_memory.items():
            confidences = [e.get("confidence", 0.0) for e in entries]
            queries = {e.get("query", "") for e in entries}
            queries.discard("")

            last_ts = None
            for e in entries:
                ts_str = e.get("timestamp")
                if ts_str:
                    try:
                        dt = datetime.fromisoformat(ts_str)
                        if last_ts is None or dt > last_ts:
                            last_ts = dt
                    except (ValueError, TypeError):
                        pass

            candidates.append(RecallCandidate(
                memory_id=mem_id,
                recall_count=len(entries),
                avg_confidence=sum(confidences) / len(confidences) if confidences else 0.0,
                unique_queries=len(queries),
                last_recalled=last_ts or now,
            ))

        return candidates

    def score_candidate(self, candidate: RecallCandidate, now: datetime | None = None) -> float:
        """4-factor weighted score: frequency, relevance, diversity, recency."""
        now = now or datetime.now(timezone.utc)

        frequency = min(candidate.recall_count / 10.0, 1.0)
        relevance = min(candidate.avg_confidence, 1.0)
        diversity = min(candidate.unique_queries / 5.0, 1.0)

        delta = now - candidate.last_recalled
        days = max(delta.total_seconds() / 86400.0, 0.0)
        recency = math.pow(0.5, days / _RECENCY_HALF_LIFE_DAYS)

        return (
            frequency * _FREQUENCY_WEIGHT
            + relevance * _RELEVANCE_WEIGHT
            + diversity * _DIVERSITY_WEIGHT
            + recency * _RECENCY_WEIGHT
        )

    def collect_reflection_promotions(self) -> list[MemoryFrontmatter]:
        """Return reflection-derived memory frontmatters not yet in MEMORY.md.

        Historical records only (mission engine deleted — prune wave 1):
        the now-removed ``mission_apply_reflection`` tool used to write
        memories with ``source_type="mission_reflection"`` that bypassed
        the recall-count / unique-queries gates. Any such memories already
        on disk still promote correctly; nothing produces new ones.
        """
        indexed_ids = set(self._index.load_ids())
        out: list[MemoryFrontmatter] = []
        for fm in self._store.list_all():
            if fm.source_type != _MISSION_REFLECTION_SOURCE_TYPE:
                continue
            if fm.id in indexed_ids:
                continue
            out.append(fm)
        return out

    def run_cycle(self, now: datetime | None = None) -> list[str]:
        """Run dreaming consolidation cycle.

        1. Build candidates from recall log
        2. Score and apply threshold gates
        3. Promote winners to MEMORY.md
        4. Promote reflection-sourced memories directly (operator-gated)
        5. Prune stale daily notes
        6. Clear promoted entries from recall log

        Returns list of promoted memory IDs.
        """
        now = now or datetime.now(timezone.utc)
        candidates = self.build_candidates(now=now)
        indexed_ids = set(self._index.load_ids())
        promoted: list[str] = []

        for c in candidates:
            if c.memory_id in indexed_ids:
                continue
            if c.recall_count < _MIN_RECALL_COUNT:
                continue
            if c.unique_queries < _MIN_UNIQUE_QUERIES:
                continue

            score = self.score_candidate(c, now=now)
            if score < _MIN_SCORE:
                continue

            read_result = self._store.read(c.memory_id, log_access=False)
            if read_result is None:
                continue

            fm, _ = read_result
            self._index.add(fm)
            promoted.append(c.memory_id)
            logger.info("Dreaming promoted %s (score=%.3f)", c.memory_id, score)

        for fm in self.collect_reflection_promotions():
            self._index.add(fm)
            promoted.append(fm.id)
            logger.info("Dreaming promoted %s (mission-reflection)", fm.id)

        self.prune_stale_daily_notes(max_age_days=_MAX_DAILY_NOTE_AGE_DAYS, now=now)
        self.sweep_missing_wikilinks()

        if promoted:
            self._clear_promoted_from_log(promoted)

        # Cap recall log so non-promoted entries don't accumulate forever.
        # Audit M2 reviewer follow-up (2026-04-29): without this trim,
        # `build_candidates` would parse a growing JSONL on every nightly
        # cycle, and the file itself would balloon on a long-lived install.
        self._trim_recall_log(
            now=now,
            max_age_days=_MAX_RECALL_LOG_AGE_DAYS,
            max_lines=_MAX_RECALL_LOG_LINES,
        )

        return promoted

    def sweep_missing_wikilinks(self) -> int:
        """Backfill [[source_path stem]] wikilinks for entries that have source_path but no wikilink.

        Skip folder-shaped paths (trailing `/` or no `.md` suffix) — those
        produce ghost nodes in Obsidian's graph since the target isn't a
        note. Only point at real markdown files.
        """
        count = 0
        for fm in self._store.list_all():
            if not fm.source_path:
                continue
            sp = fm.source_path.rstrip()
            path_part = sp.split("#", 1)[0]
            if path_part.endswith("/") or path_part.endswith("\\") or not path_part.lower().endswith(".md"):
                continue
            read_result = self._store.read(fm.id, log_access=False)
            if read_result is None:
                continue
            _, body = read_result
            if extract_wikilinks(body):
                continue
            title = Path(path_part).stem
            new_body = f"[[{title}]]\n\n{body}"
            self._store.write(fm, new_body)
            count += 1
        logger.info("Wikilink sweep: backfilled %d entries", count)
        return count

    def prune_stale_daily_notes(self, max_age_days: int = 30, now: datetime | None = None) -> int:
        """Archive daily notes older than max_age_days.

        Returns count of pruned notes.
        """
        now = now or datetime.now(timezone.utc)
        pruned = 0

        for note_path in self._store.list_daily_notes():
            try:
                date_str = note_path.stem
                note_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                age_days = (now - note_date).days
                if age_days > max_age_days:
                    dest = f"archive/{note_path.name}"
                    self._store.archive_file(str(note_path.relative_to(self._store._store_dir)), dest)
                    pruned += 1
                    logger.info("Archived stale daily note: %s", note_path.name)
            except (ValueError, OSError) as exc:
                logger.warning("Failed to prune %s: %s", note_path, exc)
        self._null_stale_daily_source_paths()
        return pruned

    def _null_stale_daily_source_paths(self) -> int:
        """Null out `source_path` on memories whose `daily/` note is gone.

        Archiving a daily note above breaks the cross-reference held by
        memories promoted from it, and memory_lint then flags them as
        stale forever. Idempotent sweep, restricted to `daily/` paths —
        other source_path shapes (vault, workshop, folder anchors) are
        out of scope. Returns count of memories repaired.
        """
        count = 0
        for fm in self._store.list_all():
            if not fm.source_path:
                continue
            cleaned = fm.source_path.strip().replace("\\", "/").split("#", 1)[0]
            if not cleaned.startswith("daily/") or cleaned.endswith("/"):
                continue
            if (self._store._store_dir / cleaned).exists():
                continue
            read_result = self._store.read(fm.id, log_access=False)
            if read_result is None:
                continue
            _, body = read_result
            repaired = fm.model_copy(update={"source_path": ""})
            if self._store.write(repaired, body, skip_wnts_check=True):
                count += 1
                logger.info("Nulled stale source_path on %s (%s)", fm.id, cleaned)
        return count

    async def run_dream_prompt(
        self,
        adapter: ModelAdapter,
        options: AdapterOptions,
        prompt: str,
    ) -> str:
        """Execute a dream task prompt via the given adapter.

        Phase 4 — the scheduler resolves adapter + options via
        ``router.resolve_for_task("dream")`` and passes them here.
        """
        from tesseract.kernel.adapters.base import ChunkType

        messages = [{"role": "user", "content": prompt}]
        parts: list[str] = []
        async for chunk in adapter.stream(messages, options=options):
            if chunk.type == ChunkType.TEXT and chunk.text:
                parts.append(chunk.text)
            elif chunk.type == ChunkType.ERROR:
                logger.warning("Dream prompt error: %s", chunk.error)
                return f"[Dream error] {chunk.error}"
        return "".join(parts)

    def _trim_recall_log(
        self,
        now: datetime,
        max_age_days: int,
        max_lines: int,
    ) -> int:
        """Drop recall entries older than `max_age_days`. If the file is
        still over `max_lines`, drop the oldest until it fits.

        Returns the number of lines removed. Idempotent — safe to call
        every cycle. Tolerates missing/malformed entries.
        """
        if not self._recall_log_path.exists():
            return 0

        cutoff = now - timedelta(days=max_age_days)
        kept: list[tuple[datetime | None, str]] = []
        with self._recall_log_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    ts_str = entry.get("timestamp")
                    ts = datetime.fromisoformat(ts_str) if ts_str else None
                    if ts is not None and ts < cutoff:
                        continue
                    kept.append((ts, line))
                except (json.JSONDecodeError, ValueError, TypeError):
                    # Tolerate malformed lines — keep them in the file rather
                    # than silently dropping operator data, but don't sort
                    # them at the head.
                    kept.append((None, line))

        if len(kept) > max_lines:
            with_ts = [(t, ln) for t, ln in kept if t is not None]
            without_ts = [ln for t, ln in kept if t is None]
            with_ts.sort(key=lambda pair: pair[0])
            overflow = len(with_ts) + len(without_ts) - max_lines
            with_ts = with_ts[overflow:]
            kept = [(t, ln) for t, ln in with_ts] + [(None, ln) for ln in without_ts]

        original_count = sum(1 for _ in self._recall_log_path.open("r", encoding="utf-8"))
        self._recall_log_path.write_text(
            "".join(ln for _, ln in kept), encoding="utf-8",
        )
        removed = original_count - len(kept)
        if removed > 0:
            logger.info("recall log trim: removed %d entries", removed)
        return removed

    def _clear_promoted_from_log(self, promoted_ids: list[str]) -> None:
        if not self._recall_log_path.exists():
            return

        promoted_set = set(promoted_ids)
        kept_lines: list[str] = []
        with self._recall_log_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    if entry.get("memory_id") not in promoted_set:
                        kept_lines.append(line)
                except json.JSONDecodeError:
                    kept_lines.append(line)

        self._recall_log_path.write_text("".join(kept_lines), encoding="utf-8")
