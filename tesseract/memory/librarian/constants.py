"""Librarian tunables and routing tables — no logic, no I/O."""

from __future__ import annotations

from tesseract.memory.types import MemoryType

MEMORY_INDEX_FILE = "MEMORY.md"
PENDING_GROWTH_FILE = "pending_growth.md"
RECENT_WINDOW_DAYS = 7
TOP_RETRIEVALS_COUNT = 20
RECENT_PROMOTIONS_COUNT = 10

# Distillation truncation — adapter prompt budget bound. 12 KB of diary
# is plenty for a 7-day window; SOUL Growth section is always tiny.
_DISTILL_DIARY_CHAR_BUDGET = 12_000
_DISTILL_BULLET_MAX_CHARS = 240
_DISTILL_TIMEOUT_S = 20.0
_DISTILL_PROMPT = """\
You are reading the assistant's private diary entries from the last few days, plus
the current `## Growth` section of its SOUL.md (the curated, durable
self-observations).

Your job: surface 0-{max_candidates} *candidate* one-line observations
that have appeared repeatedly across diary entries and feel stable enough
to belong in Growth — but DO NOT duplicate anything already there.

Rules:
- One short sentence per bullet, ≤{max_chars} chars. No explanations.
- If a pattern only shows up once, skip it. Growth is for *stable*
  observations, not single moments.
- If the diary doesn't surface anything stable, return an empty list.
  Empty is the most common case. Don't invent reasons to add bullets.
- Do not paraphrase existing Growth bullets back at me.

Output JSON only:
{{"candidates": ["bullet 1", "bullet 2"]}}

CURRENT GROWTH BULLETS:
{growth}

DIARY ENTRIES (most recent first):
{diary}
"""
# Any daily section shorter than this is treated as a fragment and skipped.
# WhatNotToSave's `_TRIVIAL_BODY_MIN_CHARS` is 80; the librarian pre-filter
# matches so the skip gets counted locally before the store.write path.
_SECTION_MIN_CHARS = 80

# Section-title tags that are runtime bookkeeping, not durable memory.
# The librarian refuses to promote these — they belong to a log stream, not
# the memory layer (see `Docs/Plan/memory-retune/` for the stream-split plan).
# Full memory-worthy surface is under `[user|feedback|project|reference|chat_digest]`.
_BOOKKEEPING_TITLE_PREFIXES = (
    "[reflect]",
    "[session_end]",
    "[auto_compact]",
    "[scheduler]",
)

_ANCHOR_FRAGILE_CHARS = {"[", "]", "/", "#", "?", "\\"}

# Type priority for the MEMORY.md Top-N ranking. Higher number ranks first.
# Curated types beat the librarian's auto-promotion bucket so real operator
# knowledge surfaces above research takeaways.
_TYPE_PRIORITY = {
    MemoryType.USER: 4,
    MemoryType.FEEDBACK: 3,
    MemoryType.PROJECT: 2,
    # CONSCIENCE sits below PROJECT — runtime self-observation is useful
    # context but should not crowd out operator-curated project notes in
    # the MEMORY.md Top-N hot index.
    MemoryType.CONSCIENCE: 1,
    MemoryType.REFERENCE: 1,
}

# M2 prefix-routing map. `[chat_digest]` folds to REFERENCE + a `chat_digest`
# tag so the digest job (M3) remains queryable via `memory_search(tags=[...])`
# without needing a new MemoryType value.
_PREFIX_TO_TYPE: dict[str, MemoryType] = {
    "user": MemoryType.USER,
    "feedback": MemoryType.FEEDBACK,
    "project": MemoryType.PROJECT,
    "reference": MemoryType.REFERENCE,
    "conscience": MemoryType.CONSCIENCE,
    "chat_digest": MemoryType.REFERENCE,
}
