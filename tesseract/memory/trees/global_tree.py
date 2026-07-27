"""AU-16 S2 — global daily digest tree.

One file per UTC date at
``<TESSERACT_HOME>/memory-store/trees/global/<YYYY-MM-DD>.md``,
listing every seal produced on that date. Operator's "what happened
today across every source" answer.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import date, datetime, timezone
from pathlib import Path

from tesseract.memory.leaf_seals import Seal
from tesseract.memory.leaves import _resolve_home

log = logging.getLogger(__name__)


def GLOBAL_TREES_ROOT() -> Path:
    return _resolve_home() / "memory-store" / "trees" / "global"


def daily_digest_path(when: date | datetime) -> Path:
    if isinstance(when, datetime):
        when = when.astimezone(timezone.utc).date()
    return GLOBAL_TREES_ROOT() / f"{when.isoformat()}.md"


def write_daily_digest(when: date | datetime, seals: list[Seal]) -> Path:
    """Render and atomically write the daily digest.

    Idempotent rewrite — passing the full ordered seal set produces a
    deterministic file. Callers (the ``DigestDailyJob``) recompute the
    whole day from the seal store on each invocation rather than
    incrementally patching.
    """
    if isinstance(when, datetime):
        when = when.astimezone(timezone.utc).date()
    target = daily_digest_path(when)
    target.parent.mkdir(parents=True, exist_ok=True)

    ordered = sorted(seals, key=lambda s: s.sealed_at, reverse=True)
    sources: dict[str, int] = {}
    total_leaves = 0
    for s in ordered:
        sources[s.source_slug] = sources.get(s.source_slug, 0) + 1
        total_leaves += s.leaf_count

    # AU-16 frontmatter contract — Obsidian's graph view picks up the
    # `global-digest` color group via the leading tag.
    lines: list[str] = [
        "---",
        "kind: global-digest",
        "state: sealed",
        "parent_tree: global",
        f"date: {when.isoformat()}",
        "tags:",
        "  - global-digest",
        "  - sealed",
        "---",
        "",
        f"# global: {when.isoformat()}",
        "",
        f"_Daily digest — {len(ordered)} seal(s), {total_leaves} leaves "
        f"across {len(sources)} source(s)._",
        "",
        "## Sources",
        "",
    ]
    for source, count in sorted(sources.items()):
        lines.append(f"- **{source}**: {count} seal(s)")
    lines.append("")
    lines.append("## Seals (newest first)")
    lines.append("")
    for s in ordered:
        lines.append(
            f"- {s.sealed_at.astimezone(timezone.utc).isoformat()} · "
            f"`{s.seal_id}` · {s.source_slug} · {s.leaf_count} leaves"
        )

    body = "\n".join(lines).rstrip() + "\n"
    tmp = target.with_name(f"{target.stem}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, target)
    return target


def read_daily_digest(when: date | datetime) -> str | None:
    path = daily_digest_path(when)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def list_digest_dates() -> list[date]:
    root = GLOBAL_TREES_ROOT()
    if not root.exists():
        return []
    out: list[date] = []
    for path in root.glob("????-??-??.md"):
        try:
            out.append(date.fromisoformat(path.stem))
        except ValueError:
            continue
    return sorted(out, reverse=True)


__all__ = [
    "GLOBAL_TREES_ROOT",
    "daily_digest_path",
    "list_digest_dates",
    "read_daily_digest",
    "write_daily_digest",
]
