"""Server-level ``instructions`` returned on MCP ``initialize``.

An MCP client is handed a tool catalog and nothing else — a CLI connecting to
the hub can see that ``memory_search`` exists but has no reason to believe it
covers anything worth fetching. The ``instructions`` string is the one channel
the spec gives a server to orient the model at session start, and it is read
once per session rather than per turn, so it can afford to carry live counts.

Numbers are computed per ``initialize`` (never cached) so a long-running Mirror
does not hand a stale picture to a CLI that connects hours later. Counting is a
directory walk plus ``stat`` — no frontmatter parsing — and the whole build is
best-effort: a failure degrades to the static text rather than failing the
handshake.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from tesseract.memory.types import MemoryType
from tesseract.paths import home_dir

log = logging.getLogger(__name__)

# Subdirectories seeded with shipped templates rather than operator content —
# counting them would advertise memory the operator never wrote.
_TEMPLATE_DIR = "_shipping"


def _memory_stats(home: Path) -> tuple[int, str | None, str | None]:
    """``(entry_count, earliest_iso_date, latest_iso_date)`` for the store.

    Walks the canonical type subdirectories recursively so operator-curated
    sub-buckets (``reference/people/``, ``project/sprints/``) are counted.
    Dates come from file mtime: the frontmatter carries authored timestamps,
    but reading them means parsing every file, and the range only needs to
    convey depth of history.
    """
    store = home / "memory-store"
    count = 0
    oldest: float | None = None
    newest: float | None = None
    for mem_type in MemoryType:
        subdir = store / mem_type.value
        if not subdir.is_dir():
            continue
        for path in subdir.rglob("*.md"):
            if _TEMPLATE_DIR in path.parts or not path.is_file():
                continue
            count += 1
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            oldest = mtime if oldest is None else min(oldest, mtime)
            newest = mtime if newest is None else max(newest, mtime)
    return count, _as_date(oldest), _as_date(newest)


def _as_date(stamp: float | None) -> str | None:
    if stamp is None:
        return None
    return datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%d")


def _vault_stats(home: Path) -> tuple[int, int]:
    """``(source_document_count, wiki_page_count)``."""
    vault = home / "vault"

    def _count(sub: str) -> int:
        root = vault / sub
        if not root.is_dir():
            return 0
        return sum(
            1
            for path in root.rglob("*")
            if path.is_file() and _TEMPLATE_DIR not in path.parts
        )

    return _count("raw"), _count("wiki")


def _memory_line(count: int, oldest: str | None, newest: str | None) -> str:
    if count == 0:
        return (
            "  memory_search — the operator's long-term memory. Currently empty; "
            "it fills as they work."
        )
    span = f"{oldest} to {newest}" if oldest and newest else "spanning their work to date"
    types = ", ".join(t.value for t in MemoryType)
    return (
        f"  memory_search — the operator's long-term memory: {count} entries, "
        f"{span}, across {types}. Retrieval is hybrid keyword + vector and "
        f"returns at most seven entries, so calling it is cheap. Do not ask the "
        f"operator for context you could have fetched."
    )


def _vault_line(sources: int, wiki_pages: int) -> str:
    if sources == 0 and wiki_pages == 0:
        return "  vault_search / vault_query — the research vault. Currently empty."
    return (
        f"  vault_search / vault_query — the research library: {sources} source "
        f"documents, {wiki_pages} compiled wiki pages. Search returns passages; "
        f"query returns a synthesised answer over the wiki."
    )


_PREAMBLE = (
    "You are connected to TESSERACT, the operator's runtime. You are not a "
    "guest process that happens to have some tools — you drive the same "
    "governed surface the resident assistant does, and your calls land in the "
    "same permission, cost, and audit trail as theirs."
)

_SURFACE = """WHAT ELSE IS HERE
  memory_save / memory_update / vault_ingest — write back what is worth
    keeping. These need the operator's approval; expect a pause, not a refusal.
  activity_* — what else is running right now, and how to cancel it.
  lane_* — drive the assistant's own claude/codex worker lanes.
  schedule_* — the recurring-job surface.
  surface_open — put something in front of the operator: a URL, file, folder,
    application, or search phrase. Reach for this when you want them to SEE
    something rather than read your description of it.
  budget_status — current spend against caps. agent_* — controller sessions."""

_FILESYSTEM = """THE FILESYSTEM
  home/ is the operator's tree — memory-store/, vault/, tars-workshop/,
  config/, workspace/. Read it freely. It is context, not clutter, and it is
  usually a faster answer than asking.

  Never write inside app/ or runtime/. app/ is installed source: it is not
  under version control on this machine, the next update deletes your edit
  without showing anyone a diff, and no reviewer ever sees it. runtime/ is
  machine state. The runtime refuses to START a process inside either tree,
  but it cannot see writes your own file tools make once you are running —
  so this one is on you.

  Write in tars-workshop/. That directory exists to be written in."""


def build_instructions() -> str:
    """Server ``instructions`` for the ``initialize`` result."""
    try:
        home = home_dir()
        count, oldest, newest = _memory_stats(home)
        sources, wiki_pages = _vault_stats(home)
        reach = "\n".join([
            "WHAT YOU CAN REACH",
            _memory_line(count, oldest, newest),
            _vault_line(sources, wiki_pages),
        ])
    except Exception:
        log.exception("mcp orientation: stat walk failed — serving instructions without counts")
        reach = (
            "WHAT YOU CAN REACH\n"
            "  memory_search — the operator's long-term memory.\n"
            "  vault_search / vault_query — the research library."
        )
    return "\n\n".join([_PREAMBLE, reach, _SURFACE, _FILESYSTEM])


__all__ = ["build_instructions"]
