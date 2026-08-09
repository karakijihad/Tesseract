"""Drift events as first-class memory records.

Each stable conscience transition lands in
`memory-store/conscience/drift/<id>.md` as a CONSCIENCE-typed memory
with structured frontmatter (`tags=['drift', <signal_name>]`) and a
body block describing the band change. The CONSCIENCE bucket separates
runtime self-observation (auto-written by the heartbeat) from
operator-curated PROJECT notes — both stay searchable through
`memory_search` and the existing pipeline (BM25, FAISS, dreaming
consolidation) treats them uniformly. Each entry carries a timestamp
so the assistant can detect "I keep drifting on the same thing" via
`count_recent_drifts`.

Same-day flap collapse: a transition arriving for a signal that already
has a same-date entry rewrites that entry's body + flips a `flapping`
tag, instead of writing a sibling. Without this, a signal that flips
ok↔bad five times in an hour would litter the store.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType, Stability

logger = logging.getLogger(__name__)

DRIFT_TAG = "drift"
FLAPPING_TAG = "flapping"
# Lives under the CONSCIENCE type's canonical subdir; `MemoryStore.write`
# routes to <store>/conscience/drift/<id>.md. The legacy "conscience"
# tag is no longer needed — type=CONSCIENCE already encodes that.
DRIFT_SUBDIR = "conscience/drift"


@dataclass(frozen=True)
class DriftWriteResult:
    memory_id: str
    path: Path
    flapping: bool
    primary_signal: str


def write_drift_entry(
    *,
    store: MemoryStore,
    transition: dict[str, Any],
    when: datetime,
) -> DriftWriteResult | None:
    """Write or update a drift memory for `transition`.

    Returns `None` if the transition is malformed (no signal name) or if
    the underlying `MemoryStore.write` blocked the entry. Writing failures
    log but don't raise — the heartbeat must not crash on a writer issue.
    """
    signal_name = _primary_signal(transition)
    if not signal_name:
        logger.warning(
            "drift writer: transition has no changed_signals; skipping write %r",
            transition,
        )
        return None

    when_utc = when.astimezone(timezone.utc)
    date_key = when_utc.date().isoformat()
    existing = _find_same_day_entry(
        store=store,
        signal_name=signal_name,
        date_key=date_key,
    )

    body = _format_body(transition, when_utc)

    if existing is not None:
        prev_fm, prev_body = existing
        merged_body = _format_flapping_body(prev_body, body, when_utc)
        merged_tags = _dedup_tags(list(prev_fm.tags) + [FLAPPING_TAG])
        base_summary = (prev_fm.summary or "").removesuffix(" (flapping)").rstrip()
        merged_summary = base_summary + " (flapping)" if base_summary else "(flapping)"
        new_fm = prev_fm.model_copy(update={
            "updated_at": when_utc,
            "tags": merged_tags,
            "summary": merged_summary[:200],
        })
        try:
            ok = store.write(new_fm, merged_body, subdir_override=DRIFT_SUBDIR)
        except Exception:
            logger.exception("drift writer: same-day update failed")
            return None
        if not ok:
            return None
        return DriftWriteResult(
            memory_id=new_fm.id,
            path=_resolve_path(store, new_fm.id),
            flapping=True,
            primary_signal=signal_name,
        )

    mem_id = MemoryFrontmatter.generate_id()
    title = _format_title(signal_name, transition, date_key)
    fm = MemoryFrontmatter(
        id=mem_id,
        type=MemoryType.CONSCIENCE,
        title=title,
        summary=_format_summary(transition),
        created_at=when_utc,
        updated_at=when_utc,
        importance=_importance_for(transition),
        tags=_dedup_tags([DRIFT_TAG, signal_name]),
        stability=Stability.ACTIVE,
        source_type="conscience_heartbeat",
    )
    try:
        ok = store.write(fm, body, subdir_override=DRIFT_SUBDIR)
    except Exception:
        logger.exception("drift writer: new entry failed")
        return None
    if not ok:
        return None
    return DriftWriteResult(
        memory_id=mem_id,
        path=_resolve_path(store, mem_id),
        flapping=False,
        primary_signal=signal_name,
    )


def count_recent_drifts(
    *,
    store: MemoryStore,
    signal_name: str,
    now: datetime,
    windows_days: tuple[int, ...] = (30, 90, 365),
) -> dict[int, int]:
    """Count prior drift entries tagged `signal_name` within each window.

    Cheap O(n) scan over the conscience memory frontmatters; fine at the
    hundreds-of-entries scale this layer operates at. Only entries
    actually tagged DRIFT_TAG are counted, so other conscience memories
    (e.g. drift_reflection notes) that happen to carry the signal name
    don't pollute the count.
    """
    if not windows_days:
        return {}
    now_utc = now.astimezone(timezone.utc)
    counts: dict[int, int] = {w: 0 for w in windows_days}
    cutoff = max(windows_days)
    earliest = now_utc - timedelta(days=cutoff)
    for fm in store.list_all(type_filter=MemoryType.CONSCIENCE):
        if DRIFT_TAG not in fm.tags or signal_name not in fm.tags:
            continue
        created = fm.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created < earliest:
            continue
        age_days = (now_utc - created).days
        for w in windows_days:
            if age_days <= w:
                counts[w] += 1
    return counts


def _primary_signal(transition: dict[str, Any]) -> str:
    """Pull the first changed-signal name; fall back to the band pair."""
    changed = transition.get("changed_signals") or []
    for entry in changed:
        name = entry.get("name") if isinstance(entry, dict) else None
        if name:
            return str(name)
    return ""


def _format_title(signal_name: str, transition: dict[str, Any], date_key: str) -> str:
    frm = transition.get("from", "?")
    to = transition.get("to", "?")
    return f"Drift: {signal_name} {frm}→{to} on {date_key}"


def _format_summary(transition: dict[str, Any]) -> str:
    summary = transition.get("summary") or {}
    ok = int(summary.get("ok", 0))
    warn = int(summary.get("warn", 0))
    bad = int(summary.get("bad", 0))
    return f"worst {transition.get('from','?')}→{transition.get('to','?')}; {ok} ok / {warn} warn / {bad} bad"


def _format_body(transition: dict[str, Any], when_utc: datetime) -> str:
    """Markdown body with wikilinks so signals cluster in the graph."""
    frm = transition.get("from", "?")
    to = transition.get("to", "?")
    summary = transition.get("summary") or {}
    changed = transition.get("changed_signals") or []
    lines: list[str] = []
    lines.append(f"# Drift event — {when_utc.isoformat()}")
    lines.append("")
    lines.append(f"- **Worst band:** {frm} → {to}")
    lines.append(
        f"- **Signal counts:** {int(summary.get('ok',0))} ok / "
        f"{int(summary.get('warn',0))} warn / {int(summary.get('bad',0))} bad"
    )
    if changed:
        lines.append("")
        lines.append("## Changed signals")
        for entry in changed:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or "?"
            entry_from = entry.get("from", "?")
            entry_to = entry.get("to", "?")
            value = entry.get("value")
            detail = entry.get("detail") or ""
            line = f"- [[{name}]] {entry_from}→{entry_to}"
            if value is not None:
                line += f" (value={value})"
            if detail:
                line += f" — {detail}"
            lines.append(line)
    lines.append("")
    lines.append(
        "Recorded by `conscience_heartbeat` on band transition. "
        "Searchable via `memory_search` for recurrence detection."
    )
    return "\n".join(lines)


def _format_flapping_body(prev_body: str, new_body: str, when_utc: datetime) -> str:
    """Append the new transition under a `## Flap update` block.

    Keeps the original body so the first-of-day record stays intact;
    flap updates accumulate chronologically below it.
    """
    suffix_parts = [
        prev_body.rstrip(),
        "",
        f"## Flap update — {when_utc.isoformat()}",
        "",
        new_body.split("\n", 1)[1].lstrip("\n") if "\n" in new_body else new_body,
    ]
    return "\n".join(suffix_parts)


def _find_same_day_entry(
    *,
    store: MemoryStore,
    signal_name: str,
    date_key: str,
) -> tuple[MemoryFrontmatter, str] | None:
    """Find a drift memory for `signal_name` created on `date_key` (UTC)."""
    for fm in store.list_all(type_filter=MemoryType.CONSCIENCE):
        if DRIFT_TAG not in fm.tags or signal_name not in fm.tags:
            continue
        created = fm.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created.astimezone(timezone.utc).date().isoformat() != date_key:
            continue
        existing = store.read(fm.id, log_access=False)
        if existing is None:
            continue
        return existing
    return None


def _resolve_path(store: MemoryStore, memory_id: str) -> Path:
    found = store.find_file(memory_id)
    if found is not None:
        return found
    return store.store_dir / DRIFT_SUBDIR / f"{memory_id}.md"


def _dedup_tags(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags:
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return out


def _importance_for(transition: dict[str, Any]) -> int:
    """Bad transitions carry more weight than warns; recoveries less than escalations."""
    frm = transition.get("from")
    to = transition.get("to")
    if to == "bad":
        return 8
    if to == "warn":
        return 6
    if frm == "bad":
        return 5
    return 4
