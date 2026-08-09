"""Server-level ``instructions`` returned on MCP ``initialize``.

An MCP client is handed a tool catalog and nothing else — a CLI connecting to
the hub can see that ``memory_search`` exists but has no reason to believe it
covers anything worth fetching. The ``instructions`` string is the one channel
the spec gives a server to orient the model at session start, and it is read
once per session rather than per turn, so it can afford to carry live counts.

Numbers are computed per ``initialize`` (never cached) so a long-running Mirror
does not hand a stale picture to a CLI that connects hours later. Counting is a
directory walk, a ``stat``, and a one-line read per file to tell a memory record
from folder documentation — the caller runs it off the event loop. The whole
build is best-effort: a failure degrades to the static text rather than failing
the handshake.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from tesseract.memory.store import list_frontmatter
from tesseract.memory.types import MemoryType
from tesseract.paths import home_dir

log = logging.getLogger(__name__)

# Subdirectories seeded with shipped templates rather than operator content —
# counting them would advertise memory the operator never wrote.
_TEMPLATE_DIR = "_shipping"

# Skeleton files `vault_manager.seed_wiki_skeleton` writes on first boot. They
# exist before any research does, so counting them advertises compiled pages
# that were never compiled.
_WIKI_CONTROL_FILES = frozenset({"INDEX.md", "ingest-log.md"})


def _memory_stats(home: Path) -> tuple[int, str | None, str | None]:
    """``(entry_count, earliest_iso_date, latest_iso_date)`` for the store.

    Counts through ``MemoryStore.list_all`` rather than a walk of its own. A
    parallel walk has to reimplement which files count — the frontmatter fence,
    the parse that follows it, the sub-buckets, the folder documentation that
    looks like a record and is not — and any of those going out of step
    advertises a number ``memory_search`` cannot deliver.

    Dates come from ``created_at`` rather than file mtime, which moves whenever
    a sync or restore touches the file.

    Shipped templates need no filtering here: they carry no frontmatter fence,
    so ``list_all`` already skips them for the same reason it skips README.md.

    Goes through ``list_frontmatter`` rather than ``MemoryStore``: constructing
    the store calls ``_ensure_dirs``, which creates any missing subdirectory.
    A handshake must not do that — answering "how much memory is there" is a
    read, and a read that materialises what it is counting is a write wearing
    a question's clothes.
    """
    entries = list_frontmatter(home / "memory-store")
    if not entries:
        return 0, None, None
    stamps = [fm.created_at for fm in entries]
    return len(entries), _as_date(min(stamps)), _as_date(max(stamps))


def _as_date(stamp: datetime | None) -> str | None:
    if stamp is None:
        return None
    return stamp.strftime("%Y-%m-%d")


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular}" if count == 1 else f"{count} {plural}"


def _vault_stats(home: Path) -> tuple[int, int]:
    """``(source_document_count, wiki_page_count)``."""
    vault = home / "vault"

    def _count(sub: str, skip: frozenset[str] = frozenset()) -> int:
        root = vault / sub
        if not root.is_dir():
            return 0
        return sum(
            1
            for path in root.rglob("*")
            if path.is_file()
            and _TEMPLATE_DIR not in path.parts
            and path.name not in skip
        )

    return _count("raw"), _count("wiki", _WIKI_CONTROL_FILES)


def _memory_line(count: int, oldest: str | None, newest: str | None) -> str:
    if count == 0:
        return (
            "  memory_search — the operator's long-term memory. Currently empty; "
            "it fills as they work."
        )
    span = f"{oldest} to {newest}" if oldest and newest else "spanning their work to date"
    types = ", ".join(t.value for t in MemoryType)
    return (
        f"  memory_search — the operator's long-term memory: {_plural(count, 'entry', 'entries')}, "
        f"{span}, across {types}. Retrieval is hybrid keyword + vector and "
        f"returns at most seven entries, so calling it is cheap. Do not ask the "
        f"operator for context you could have fetched."
    )


def _vault_line(sources: int, wiki_pages: int) -> str:
    if sources == 0 and wiki_pages == 0:
        return "  vault_search / vault_query — the research vault. Currently empty."
    return (
        f"  vault_search / vault_query — the research library: "
        f"{_plural(sources, 'source document', 'source documents')}, "
        f"{_plural(wiki_pages, 'compiled wiki page', 'compiled wiki pages')}. "
        f"Search returns passages; query returns a synthesised answer over the wiki."
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
  activity_* — what else is running right now, and how to cancel it. For work
    you are waiting on, open the GET SSE stream on this endpoint rather than
    polling activity_list — a snapshot misses whatever started and finished
    between two calls.
  lane_* — drive the assistant's own claude/codex worker lanes.
  schedule_* — the recurring-job surface.
  surface_open — put something in front of the operator: a URL, file, folder,
    application, or search phrase. Reach for this when you want them to SEE
    something rather than read your description of it.
  budget_status — current spend against caps. agent_* — controller sessions."""

_FILESYSTEM = """THE FILESYSTEM
  home/ is the operator's tree — memory-store/, vault/, workshop/,
  config/, workspace/. Read it freely. It is context, not clutter, and it is
  usually a faster answer than asking.

  Never write inside app/ or runtime/. app/ is installed source — a clone the
  updater manages, not a working copy. An edit there is outside the review
  that every other change goes through, and the next update overwrites it
  without showing anyone a diff. runtime/ is machine state. The runtime
  refuses to START a process inside either tree, but it cannot see writes your
  own file tools make once you are running — so this one is on you.

  Write in workshop/. That directory exists to be written in."""

# The store only stays shared if connecting models actually write to it. Every
# other block here describes what exists; this one asks for something, and it
# goes last so it reads as the instruction the session closes on. Deliberately
# narrow about what to save — an open invitation produces transcript dumps,
# which cost more to retrieve past than they are worth.
_CLOSING = """BEFORE YOU FINISH
  Save what outlived this session. A decision and the reason behind it, a
  constraint you discovered the hard way, a preference the operator stated,
  where something non-obvious lives — memory_save takes these.

  Not the transcript, not a summary of what you did, not anything already
  recoverable from the code or its history. Someone else picks up this work
  next — possibly the resident assistant, possibly the other CLI — and reads
  the same store you just read. Leave it better than you found it."""


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
    return "\n\n".join([_PREAMBLE, reach, _SURFACE, _FILESYSTEM, _CLOSING])


__all__ = ["build_instructions"]
