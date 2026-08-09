"""MEMORY.md summary stage — counts, recent promotions, and the Top-N
importance ranking rendered into `<store_dir>/MEMORY.md`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tesseract.memory.librarian.constants import (
    MEMORY_INDEX_FILE,
    RECENT_WINDOW_DAYS,
    TOP_RETRIEVALS_COUNT,
    _TYPE_PRIORITY,
)
from tesseract.memory.librarian.utils import _atomic_write, _clip_words, _is_bookkeeping_entry
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType


class SummaryMixin:
    """MEMORY.md index rendering. Expects `_store` provided by the composed
    `Librarian`.
    """

    _store: MemoryStore

    def _count_by_type(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for mem_type in MemoryType:
            out[mem_type.value] = len(self._store.list_all(type_filter=mem_type))
        return out

    def _recent_entries(self, days: int, limit: int) -> list[MemoryFrontmatter]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        recent = [
            fm for fm in self._store.list_all()
            if fm.created_at >= cutoff and not _is_bookkeeping_entry(fm)
        ]
        recent.sort(key=lambda fm: fm.created_at, reverse=True)
        return recent[:limit]

    def _top_by_importance(self, limit: int) -> tuple[list[MemoryFrontmatter], int]:
        # Type priority keeps curated memories (user/feedback/project) above
        # auto-promoted references in the Top-N surface. Legacy bookkeeping
        # entries — both title-prefixed (reflect/session_end/…) and the
        # tag-based catch for librarian-promoted REFERENCE stubs whose title
        # lost its bracket — are excluded so MEMORY.md shows signal, not
        # runtime logs. `filtered` counts how many were dropped so the
        # librarian-pass log shows the remaining pollution at a glance.
        all_entries = self._store.list_all()
        kept = [fm for fm in all_entries if not _is_bookkeeping_entry(fm)]
        kept.sort(key=lambda fm: (_TYPE_PRIORITY[fm.type], fm.importance, fm.created_at), reverse=True)
        filtered = len(all_entries) - len(kept)
        return kept[:limit], filtered

    def _link_path(self, fm: MemoryFrontmatter) -> str:
        """Resolve the actual on-disk path for `fm` and return it relative
        to store_dir as a POSIX string.

        Operators curate sub-buckets (`reference/people/`, `project/sprints/`).
        A naive `{type}/{id}.md` link breaks for every nested entry. Walk the
        store via `find_file` so the link points at the real file regardless
        of folder depth. Falls back to the canonical layout if the file is
        missing — keeps MEMORY.md renderable on a half-deleted store.
        """
        path = self._store.find_file(fm.id)
        if path is None:
            return f"{fm.type.value}/{fm.id}.md"
        return path.relative_to(self._store.store_dir).as_posix()

    def _write_memory_index(
        self,
        *,
        top: list[MemoryFrontmatter],
        recent: list[MemoryFrontmatter],
        counts: dict[str, int],
    ) -> None:
        path = self._store.store_dir / MEMORY_INDEX_FILE
        now = datetime.now(timezone.utc).date().isoformat()

        lines: list[str] = []
        lines.append("# TESSERACT Memory Index")
        lines.append("")
        lines.append(f"**Last updated:** {now} (librarian pass)")
        lines.append("**Updated by:** librarian (manual via `/reflect`; auto via heartbeat when wired)")
        lines.append("")
        lines.append(
            "Top-level curated synthesis — what the assistant durably knows about the "
            "operator, projects, tools, and concepts. The raw capture layer "
            "lives in `daily/YYYY-MM-DD.md`; canonical entries live in the "
            "per-type subdirs. This file surfaces what matters right now."
        )
        lines.append("")

        lines.append("## Counts")
        lines.append("")
        for name, n in counts.items():
            lines.append(f"- `{name}/`: {n}")
        lines.append("")

        lines.append(f"## Top {TOP_RETRIEVALS_COUNT} by importance")
        lines.append("")
        if not top:
            lines.append("*(empty — librarian ran before any memories exist)*")
        else:
            for fm in top:
                lines.append(f"- [{fm.title}]({self._link_path(fm)}) — importance {fm.importance} · {_clip_words(fm.summary, 80)}")
        lines.append("")

        lines.append(f"## Recent promotions (last {RECENT_WINDOW_DAYS} days)")
        lines.append("")
        if not recent:
            lines.append("*(empty — no memories created in the recent window)*")
        else:
            for fm in recent:
                age_days = (datetime.now(timezone.utc) - fm.created_at).days
                lines.append(f"- [{fm.title}]({self._link_path(fm)}) — {age_days}d ago · {_clip_words(fm.summary, 80)}")
        lines.append("")

        lines.append("## Anchors")
        lines.append("")
        lines.append("See `tesseract/memory/entities.yaml` — operator-defined taxonomy the librarian enriches within.")
        lines.append("")

        _atomic_write(path, "\n".join(lines))
