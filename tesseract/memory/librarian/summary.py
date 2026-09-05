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
from tesseract.memory.index import render_index_row, resolve_link_path
from tesseract.memory.librarian.utils import _atomic_write, _is_bookkeeping_entry
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType
from tesseract.lib import clock


#: Heading per memory type, in reading order. Named for what the reader is
#: looking for rather than for the folder they live in.
_TYPE_HEADINGS: dict[MemoryType, str] = {
    MemoryType.FEEDBACK: "How to work with them",
    MemoryType.USER: "About them",
    MemoryType.PROJECT: "Projects and decisions",
    MemoryType.REFERENCE: "Reference",
    MemoryType.CONSCIENCE: "What the runtime noticed",
}


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
        """The librarian's name for `index.resolve_link_path`.

        Both writers of MEMORY.md need the same answer to where a row points.
        """
        return resolve_link_path(self._store.store_dir, fm)

    def _write_memory_index(
        self,
        *,
        top: list[MemoryFrontmatter],
        recent: list[MemoryFrontmatter],
        counts: dict[str, int],
    ) -> None:
        path = self._store.store_dir / MEMORY_INDEX_FILE
        # A date a person reads off the index, so it is their date.
        now = clock.today().isoformat()

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

        # Grouped by kind, not ranked into one list: someone looking for what
        # they taught the assistant looks under feedback rather than reading a
        # ranking to find out which entries those are. Ranking still selects
        # what gets in (`top`); it does not decide the shape of the page.
        lines.append(f"## What I know ({len(top)} of {sum(counts.values())})")
        lines.append("")
        if not top:
            lines.append("*(empty — librarian ran before any memories exist)*")
        for mem_type in _TYPE_HEADINGS:
            group = [fm for fm in top if fm.type is mem_type]
            if not group:
                continue
            lines.append(f"### {_TYPE_HEADINGS[mem_type]}")
            lines.append("")
            for fm in group:
                lines.append(render_index_row(self._store.store_dir, fm))
            lines.append("")

        lines.append(f"## Newest (last {RECENT_WINDOW_DAYS} days)")
        lines.append("")
        if not recent:
            lines.append("*(empty — no memories created in the recent window)*")
        else:
            for fm in recent:
                age_days = (datetime.now(timezone.utc) - fm.created_at).days
                lines.append(render_index_row(self._store.store_dir, fm, prefix=f"{age_days}d ago"))
        lines.append("")

        lines.append("## Anchors")
        lines.append("")
        lines.append("See `tesseract/memory/entities.yaml` — operator-defined taxonomy the librarian enriches within.")
        lines.append("")

        _atomic_write(path, "\n".join(lines))
