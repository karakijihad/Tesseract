"""Personality distillation stage — read recent diary + SOUL Growth, ask
the adapter for stable candidate observations, write `pending_growth.md`.
The librarian never edits SOUL.md itself.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.memory.embeddings import EmbeddingIndex
from tesseract.memory.librarian.constants import (
    PENDING_GROWTH_FILE,
    RECENT_WINDOW_DAYS,
    _DISTILL_BULLET_MAX_CHARS,
    _DISTILL_DIARY_CHAR_BUDGET,
    _DISTILL_PROMPT,
    _DISTILL_TIMEOUT_S,
)
from tesseract.memory.librarian.utils import _atomic_write, _normalize, _parse_candidates
from tesseract.memory.store import MemoryStore

logger = logging.getLogger(__name__)


class DistillationMixin:
    """SOUL Growth candidate distillation. Expects `_store`, `_adapter`,
    `_adapter_options` provided by the composed `Librarian`.
    """

    _store: MemoryStore
    _embeddings: EmbeddingIndex | None
    _adapter: ModelAdapter | None
    _adapter_options: AdapterOptions | None

    async def distill_personality_candidates(
        self,
        soul_path: Path,
        *,
        days: int = RECENT_WINDOW_DAYS,
        max_candidates: int = 3,
        adapter_chain: list[tuple[ModelAdapter, AdapterOptions]] | None = None,
    ) -> dict:
        """Read the last `days` of diary entries + current SOUL.md Growth
        section, ask the adapter to surface stable observations, write 0-N
        candidates to `<store_dir>/pending_growth.md`.

        Returns `{candidates: int, reason?: str}`. Never raises — adapter
        failures, missing diary, or missing soul return `{candidates: 0,
        reason: ...}` so the heartbeat job can keep going.

        `adapter_chain` lets callers (the heartbeat scheduler job) route
        the call through any role's primary+fallbacks. When omitted the
        librarian's construction-time `(_adapter, _adapter_options)` is
        used as a single-element chain — preserves the legacy behavior
        for direct REPL callers and existing tests.

        The librarian itself never edits SOUL.md. `pending_growth.md` is a
        proposal surface — the operator (or TARS via `soul_growth_propose`)
        is the only path that mutates Growth.
        """
        chain = adapter_chain
        if chain is None:
            if self._adapter is None:
                return {"candidates": 0, "reason": "adapter_offline"}
            chain = [(self._adapter, self._adapter_options or AdapterOptions())]
        if not chain:
            return {"candidates": 0, "reason": "adapter_offline"}

        diary_text = self._read_recent_diary(days=days)
        if not diary_text:
            self._write_pending_growth([], reason="no_diary")
            return {"candidates": 0, "reason": "no_diary"}

        try:
            growth_bullets = self._read_soul_growth(soul_path)
        except FileNotFoundError:
            return {"candidates": 0, "reason": "soul_missing"}

        prompt = _DISTILL_PROMPT.format(
            max_candidates=max_candidates,
            max_chars=_DISTILL_BULLET_MAX_CHARS,
            growth="\n".join(f"- {b}" for b in growth_bullets) or "*(none)*",
            diary=diary_text,
        )

        raw = ""
        last_reason = "adapter_error"
        for index, (adapter, options) in enumerate(chain):
            label = f"{options.provider or '?'}/{options.model or '?'}"
            try:
                raw = await asyncio.wait_for(
                    adapter.generate(prompt, options or AdapterOptions()),
                    timeout=_DISTILL_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "distill: %s timed out after %.1fs (chain idx=%d)",
                    label, _DISTILL_TIMEOUT_S, index,
                )
                last_reason = "adapter_timeout"
                continue
            except Exception as exc:  # noqa: BLE001 — best-effort distillation
                logger.warning(
                    "distill: %s call failed (%s) (chain idx=%d)", label, exc, index,
                )
                last_reason = "adapter_error"
                continue
            if raw and raw.strip():
                if index > 0:
                    logger.info("distill: fell back to chain idx=%d (%s)", index, label)
                break
            logger.warning("distill: %s returned empty (chain idx=%d)", label, index)
            last_reason = "adapter_empty"
            raw = ""
        if not raw:
            return {"candidates": 0, "reason": last_reason}

        candidates = _parse_candidates(raw, max_candidates=max_candidates)
        # Drop anything that paraphrases an existing Growth bullet.
        existing_norm = {_normalize(b) for b in growth_bullets}
        deduped = [c for c in candidates if _normalize(c) not in existing_norm]

        self._write_pending_growth(deduped)
        return {"candidates": len(deduped)}

    def _read_recent_diary(self, *, days: int) -> str:
        """Read diary files from the last `days` (most recent first), join
        with file headers. Returns "" if the diary dir is missing or empty.
        """
        diary_dir = self._store.store_dir / "diary"
        if not diary_dir.exists():
            return ""
        cutoff = date.today() - timedelta(days=days)
        files: list[tuple[str, Path]] = []
        for path in diary_dir.glob("*.md"):
            try:
                stem_date = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if stem_date < cutoff:
                continue
            files.append((path.stem, path))
        if not files:
            return ""
        files.sort(key=lambda x: x[0], reverse=True)

        chunks: list[str] = []
        budget = _DISTILL_DIARY_CHAR_BUDGET
        for stem, path in files:
            try:
                body = path.read_text(encoding="utf-8")
            except OSError:
                continue
            block = f"--- {stem} ---\n{body.strip()}"
            if len(block) > budget:
                chunks.append(block[:budget])
                break
            chunks.append(block)
            budget -= len(block)
            if budget <= 0:
                break
        return "\n\n".join(chunks)

    @staticmethod
    def _read_soul_growth(soul_path: Path) -> list[str]:
        """Return existing `- bullet` lines under SOUL.md `## Growth`.
        Raises FileNotFoundError if the file is missing.
        """
        text = soul_path.read_text(encoding="utf-8")
        match = re.search(r"^## Growth\s*\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
        if not match:
            return []
        bullets: list[str] = []
        for line in match.group(1).splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                bullets.append(stripped[2:].strip())
        return bullets

    def _write_pending_growth(self, candidates: list[str], *, reason: str | None = None) -> None:
        """Overwrite `<store_dir>/pending_growth.md` with the latest pass.
        Always written — empty result wipes stale candidates so the
        operator never sees outdated proposals.
        """
        path = self._store.store_dir / PENDING_GROWTH_FILE
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        lines = [
            "# Pending Growth Candidates",
            "",
            f"**Distilled at:** {now}",
            "**Source:** librarian heartbeat → diary distillation",
            "",
            "Candidate observations the librarian noticed across recent diary "
            "entries. Not yet in SOUL.md — TARS reviews these at session-end "
            "reflection and calls `soul_growth_propose` if any feel stable.",
            "",
        ]
        if not candidates:
            lines.append(f"*(no candidates this pass{f' — {reason}' if reason else ''})*")
        else:
            lines.extend(f"- {c}" for c in candidates)
        lines.append("")
        _atomic_write(path, "\n".join(lines))
