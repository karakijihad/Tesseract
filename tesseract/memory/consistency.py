"""Consistency checker for memory store.

Runs at startup and can be triggered manually. Detects and repairs:
- Orphaned files (in store but not in MEMORY.md)
- Stale pointers (in MEMORY.md but file missing)
- Missing embeddings (in store but not in FAISS)
- Stale embeddings (in FAISS but not in store)
- FTS orphans/stale entries
- MEMORY.md over cap
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from tesseract.memory.embeddings import EmbeddingIndex
from tesseract.memory.fts_index import FTSIndex
from tesseract.memory.index import MemoryIndex
from tesseract.memory.store import MemoryStore

logger = logging.getLogger(__name__)

# FTS-only rows for daily notes (`daily_YYYY-MM-DD`), not backed by the
# canonical store. Rebuilds of the shared FTS table must preserve them.
DAILY_FTS_PREFIX = "daily_"


@dataclass
class ConsistencyReport:
    orphaned_files: list[str] = field(default_factory=list)
    stale_pointers: list[str] = field(default_factory=list)
    missing_embeddings: list[str] = field(default_factory=list)
    stale_embeddings: list[str] = field(default_factory=list)
    missing_fts: list[str] = field(default_factory=list)
    stale_fts: list[str] = field(default_factory=list)
    embedding_fragmentation: float = 0.0
    repairs_made: int = 0


class ConsistencyChecker:
    def __init__(
        self,
        store: MemoryStore,
        index: MemoryIndex,
        store_dir: Path,
        embeddings: EmbeddingIndex | None = None,
        fts_index: FTSIndex | None = None,
        vault_root: Path | None = None,
    ) -> None:
        self._store = store
        self._index = index
        self._store_dir = store_dir
        self._embeddings = embeddings
        self._fts_index = fts_index
        self._vault_root = vault_root

    def _vault_chunk_source_exists(self, chunk_id: str) -> bool:
        """Check if the vault file backing a vault: chunk ID still exists."""
        if self._vault_root is None:
            return True  # Can't verify without vault root — assume valid
        # chunk_id format: vault:{vault_rel_path}:chunk_{N}
        parts = chunk_id.split(":chunk_")
        if len(parts) != 2:
            return False
        vault_rel_path = parts[0].removeprefix("vault:")
        return (self._vault_root / vault_rel_path).exists()

    def check(self) -> ConsistencyReport:
        report = ConsistencyReport()

        index_ids = set(self._index.load_ids())
        store_fms = self._store.list_all()
        store_ids = {fm.id for fm in store_fms}

        for mem_id in store_ids - index_ids:
            report.orphaned_files.append(mem_id)

        for mem_id in index_ids - store_ids:
            report.stale_pointers.append(mem_id)

        if self._embeddings is not None:
            # `snapshot_ids()` takes the embeddings lock — direct access to
            # `_id_to_pos` would race with concurrent rebuild/compact
            # (audit-fix M5).
            embedded_ids = set(self._embeddings.snapshot_ids())

            for mem_id in store_ids - embedded_ids:
                report.missing_embeddings.append(mem_id)

            for mem_id in embedded_ids - store_ids:
                if mem_id.startswith("vault:"):
                    if not self._vault_chunk_source_exists(mem_id):
                        report.stale_embeddings.append(mem_id)
                    continue
                report.stale_embeddings.append(mem_id)

            report.embedding_fragmentation = self._embeddings.fragmentation

        if self._fts_index is not None:
            fts_ids = set(self._fts_index.all_ids())

            for mem_id in store_ids - fts_ids:
                report.missing_fts.append(mem_id)

            for mem_id in fts_ids - store_ids:
                if mem_id.startswith(DAILY_FTS_PREFIX):
                    continue  # Daily note FTS entries are not backed by store
                if mem_id.startswith("vault:"):
                    if not self._vault_chunk_source_exists(mem_id):
                        report.stale_fts.append(mem_id)
                    continue
                report.stale_fts.append(mem_id)

        if report.orphaned_files:
            logger.warning("Found %d orphaned memory files", len(report.orphaned_files))
        if report.stale_pointers:
            logger.warning("Found %d stale MEMORY.md pointers", len(report.stale_pointers))
        if report.missing_embeddings:
            logger.warning("Found %d memories missing embeddings", len(report.missing_embeddings))
        if report.stale_embeddings:
            logger.warning("Found %d stale embeddings", len(report.stale_embeddings))
        if report.missing_fts:
            logger.warning("Found %d memories missing FTS entries", len(report.missing_fts))
        if report.stale_fts:
            logger.warning("Found %d stale FTS entries", len(report.stale_fts))

        return report

    def _migrate_legacy_faiss(self) -> None:
        """Move FAISS index from legacy episodes/ dir to derived/ if needed."""
        legacy = self._store_dir / "episodes" / "index.faiss"
        target = self._store_dir / "derived" / "index.faiss"
        if legacy.exists() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy), str(target))
            logger.info("Migrated FAISS index from episodes/ to derived/")
            # Also move id_map if present
            legacy_map = self._store_dir / "episodes" / "id_map.json"
            target_map = self._store_dir / "derived" / "id_map.json"
            if legacy_map.exists() and not target_map.exists():
                shutil.move(str(legacy_map), str(target_map))
                logger.info("Migrated id_map.json from episodes/ to derived/")

    async def repair(self) -> ConsistencyReport:
        self._migrate_legacy_faiss()
        report = self.check()

        for mem_id in report.stale_pointers:
            self._index.remove(mem_id)
            report.repairs_made += 1
            logger.info("Removed stale pointer: %s", mem_id)

        store_fms = self._store.list_all()
        fm_by_id = {fm.id: fm for fm in store_fms}

        for mem_id in report.orphaned_files:
            fm = fm_by_id.get(mem_id)
            if fm:
                self._index.add(fm)
                report.repairs_made += 1
                logger.info("Added orphaned file to index: %s", mem_id)

        if self._embeddings is not None:
            for mem_id in report.stale_embeddings:
                self._embeddings.remove(mem_id)
                report.repairs_made += 1
                logger.info("Removed stale embedding: %s", mem_id)

            for mem_id in report.missing_embeddings:
                read_result = self._store.read(mem_id, log_access=False)
                if read_result:
                    _, body = read_result
                    if await self._embeddings.add(mem_id, body):
                        report.repairs_made += 1
                        logger.info("Added missing embedding: %s", mem_id)

            if self._embeddings.fragmentation > 0.3:
                await self._embeddings.compact()
                report.repairs_made += 1
                logger.info("Compacted FAISS index (fragmentation was %.1f%%)",
                            report.embedding_fragmentation * 100)

        if self._fts_index is not None:
            for mem_id in report.stale_fts:
                self._fts_index.delete(mem_id)
                report.repairs_made += 1
                logger.info("Removed stale FTS entry: %s", mem_id)

            for mem_id in report.missing_fts:
                read_result = self._store.read(mem_id, log_access=False)
                if read_result:
                    fm, body = read_result
                    self._fts_index.add(mem_id, fm.title, body)
                    report.repairs_made += 1
                    logger.info("Added missing FTS entry: %s", mem_id)

            # Reindex daily notes that are not yet in FTS
            current_fts_ids = set(self._fts_index.all_ids())
            for note_path in self._store.list_daily_notes():
                date_str = note_path.stem  # YYYY-MM-DD
                fts_id = f"{DAILY_FTS_PREFIX}{date_str}"
                if fts_id not in current_fts_ids:
                    content = note_path.read_text(encoding="utf-8")
                    self._fts_index.add(fts_id, date_str, content)
                    report.repairs_made += 1
                    logger.info("Indexed daily note into FTS: %s", fts_id)

        if report.repairs_made:
            logger.info("Consistency repair complete: %d fixes", report.repairs_made)

        return report
