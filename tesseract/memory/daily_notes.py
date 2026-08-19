"""Append structured sections to `memory-store/daily/YYYY-MM-DD.md`.

Single helper shared by the scheduler's DailyWriterJob and the Mirror WS
hooks (`[scheduler]`, `[session_end]`, `[auto_compact]`, `[reflect]`). All
four layers share file format, idempotency semantics, and the librarian's
80-char section-body floor, so they share one implementation.

"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

# Librarian's `_SECTION_MIN_CHARS = 80`. Sections shorter than this are
# dropped by the promotion pass — zero-turn session_end entries and the
# "no scheduled runs" rollup both need padding to survive.
_LIBRARIAN_MIN_BODY = 80
# Long enough that any short body + this pad clears the 80-char floor with slack.
_DAILY_PAD = (
    "\n\n<!-- padded to meet the librarian's 80-char section floor so this "
    "deterministic section still surfaces in the promotion pass -->"
)


def _daily_frontmatter(date_str: str) -> str:
    """AU-16 frontmatter for a fresh daily-note file. Written exactly
    once at file creation; subsequent ``append_section`` calls leave it
    alone. Obsidian's graph view picks up the ``daily-note`` color group
    via the leading tag.
    """
    return (
        "---\n"
        "kind: daily-note\n"
        "state: active\n"
        "parent_tree: daily\n"
        f"date: {date_str}\n"
        "tags:\n  - daily-note\n  - active\n"
        "---\n"
    )


def section_exists(*, probe: str, daily_dir: Path, date: datetime) -> bool:
    """Whether `<daily_dir>/YYYY-MM-DD.md` already contains `probe`.

    The same test `append_section` makes before writing, exposed so a caller
    can make it BEFORE doing expensive work. `chat_digest` paid for a model
    call and then discovered the day was already digested; with a catch-up
    walking several missed days that became several paid calls producing
    nothing.
    """
    target = daily_dir / f"{date.strftime('%Y-%m-%d')}.md"
    if not target.exists():
        return False
    try:
        return probe in target.read_text(encoding="utf-8")
    except OSError:
        return False


def append_section(
    *,
    header: str,
    body: str,
    daily_dir: Path,
    date: datetime | None = None,
    idempotency_probe: str | None = None,
    pad_short: bool = False,
) -> bool:
    """Append a markdown section to `<daily_dir>/YYYY-MM-DD.md`.

    Returns True on write, False when `idempotency_probe` matches existing
    content. On the first write of the day, prepends the AU-16 daily-note
    frontmatter so the file color-groups correctly when opened in
    Obsidian. Never raises on ordinary filesystem misses — the caller
    path (WS hook / cron job) wraps this in its own guard.
    """
    when = date if date is not None else datetime.now(timezone.utc)
    date_str = when.strftime("%Y-%m-%d")
    target = daily_dir / f"{date_str}.md"
    daily_dir.mkdir(parents=True, exist_ok=True)

    file_exists = target.exists()
    if idempotency_probe is not None and file_exists:
        existing = target.read_text(encoding="utf-8")
        if idempotency_probe in existing:
            return False

    final_body = body
    if pad_short and len(final_body) < _LIBRARIAN_MIN_BODY:
        final_body = f"{final_body}{_DAILY_PAD}"

    with target.open("a", encoding="utf-8") as fh:
        if not file_exists:
            fh.write(_daily_frontmatter(date_str))
        fh.write(f"\n\n{header}\n{final_body}\n")
    return True
