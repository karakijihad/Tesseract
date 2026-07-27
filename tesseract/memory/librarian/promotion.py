"""Daily-note promotion stage — scan `daily/*.md`, promote promotable
sections into canonical subdirs via `store.write()` with dedupe + WNTS.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from pathlib import Path

from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.memory import dedupe
from tesseract.memory.classifier import classify_section
from tesseract.memory.embeddings import EmbeddingIndex
from tesseract.memory.librarian.constants import _PREFIX_TO_TYPE, _SECTION_MIN_CHARS
from tesseract.memory.librarian.utils import (
    _anchor_slug,
    _clip_words,
    _extract_type_prefix,
    _is_bookkeeping_title,
    _parse_daily_sections,
)
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType

logger = logging.getLogger(__name__)


class PromotionMixin:
    """Daily-note → canonical-store promotion. Expects `_store`, `_embeddings`,
    `_adapter`, `_adapter_options` provided by the composed `Librarian`.
    """

    _store: MemoryStore
    _embeddings: EmbeddingIndex | None
    _adapter: ModelAdapter | None
    _adapter_options: AdapterOptions | None

    async def _promote_daily(self) -> tuple[int, int, int, int]:
        """Iterate daily files older than today, promote promotable sections.

        Returns `(promoted, deduped, merged, skipped)`. Today's daily file
        is left alone — it's still being appended to.
        """
        today = date.today().isoformat()
        promoted = deduped = merged = skipped = 0

        # Offload the synchronous full-store scan off the event loop (audit
        # 2026-07-18, MED — write/promotion path). Fetched once per pass and
        # passed to each `dedupe.check_with_title` below. `list_all` is
        # read-only + thread-safe.
        existing_entries = await asyncio.to_thread(self._store.list_all)
        already_promoted = {fm.source_path for fm in existing_entries if fm.source_path}

        for daily_path in self._store.list_daily_notes():
            if daily_path.stem == today:
                continue

            try:
                text = daily_path.read_text(encoding="utf-8")
            except OSError as e:
                logger.warning("librarian: failed to read %s: %s", daily_path, e)
                continue

            for title, body in _parse_daily_sections(text):
                if len(body) < _SECTION_MIN_CHARS:
                    skipped += 1
                    continue
                if _is_bookkeeping_title(title):
                    # Runtime bookkeeping — lives in the log stream, never memory.
                    skipped += 1
                    continue

                source_anchor = self._source_anchor(daily_path, title)
                if source_anchor in already_promoted:
                    deduped += 1
                    continue

                prefix = _extract_type_prefix(title)
                prefix_type = _PREFIX_TO_TYPE.get(prefix.lower()) if prefix else None
                proceed, existing_id, reason = await dedupe.check_with_title(
                    title,
                    body,
                    self._store,
                    self._embeddings,
                    existing=existing_entries,
                    new_type=prefix_type,
                )
                if not proceed:
                    if reason == "cosine_merge" and existing_id:
                        if self._store.update_body(existing_id, body):
                            merged += 1
                        else:
                            skipped += 1
                    else:
                        deduped += 1
                    continue

                written = await self._write_section(title, body, daily_path, source_anchor)
                if written is not None:
                    promoted += 1
                    already_promoted.add(source_anchor)
                    existing_entries.append(written)
                else:
                    skipped += 1

        return promoted, deduped, merged, skipped

    def _source_anchor(self, daily_path: Path, title: str) -> str:
        base = daily_path.relative_to(self._store.store_dir).as_posix()
        slug = _anchor_slug(title) if title else "anon"
        return f"{base}#{slug}"

    def _skip_unclassifiable(self, title: str, source_anchor: str) -> None:
        self._store.log_event("writes.jsonl", {
            "source_path": source_anchor,
            "title": title,
            "status": "skipped",
            "reason": "unclassifiable",
        })
        return None

    async def _write_section(
        self, title: str, body: str, daily_path: Path, source_anchor: str
    ) -> MemoryFrontmatter | None:
        """Write the section as a new memory; return its frontmatter on
        success, else None (skipped or classifier declined). The caller
        appends the returned fm into the within-pass `existing_entries`
        cache so the title-dedupe guard catches later same-pass siblings.
        """
        prefix = _extract_type_prefix(title)
        extra_tags: list[str] = []
        mem_type: MemoryType | None = None

        if prefix is not None:
            token = prefix.lower()
            mem_type = _PREFIX_TO_TYPE.get(token)
            if token == "chat_digest":
                extra_tags.append("chat_digest")

        if mem_type is None:
            if self._adapter is None:
                return self._skip_unclassifiable(title, source_anchor)
            classified, _confidence = await classify_section(
                title,
                body,
                self._adapter,
                options=self._adapter_options,
            )
            if classified is None:
                return self._skip_unclassifiable(title, source_anchor)
            mem_type = classified

        now = datetime.now(timezone.utc)
        fm = MemoryFrontmatter(
            id=MemoryFrontmatter.generate_id(),
            type=mem_type,
            title=title or f"daily-{daily_path.stem}",
            summary=_clip_words(body, 100),
            created_at=now,
            updated_at=now,
            importance=5,
            tags=["promoted", f"daily:{daily_path.stem}", *extra_tags],
            source_session="librarian",
            source_path=source_anchor,
            source_url="",
            source_type="consolidation",
        )
        if self._store.write(fm, body):
            return fm
        return None
