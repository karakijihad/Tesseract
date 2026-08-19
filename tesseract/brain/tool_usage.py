"""One line per tool call, so the working set can be a measurement.

Which tools are pre-loaded into every turn's schema payload is a dial, and a
dial with no gauge gets set once from intuition and never moved. Nothing in the
runtime counted tool calls: `logs/audit/pc.jsonl` is browser verbs only,
`logs/tokenjuice/audit.jsonl` fires only for the few tools carrying a
tokenjuice rule, and the session transcripts hold the calls but are compacted,
pruned and never meant as a ledger.

So this records the one fact the decision needs — **which tool, in which
session** — and nothing else. No inputs, no outputs, no result text: a usage
ledger that carries payloads is a second copy of the transcript with the
operator's data in it, kept for a purpose that never needs to read it.

Rank by distinct sessions rather than by calls. One loop calling `lane_read`
four hundred times is one session's worth of evidence, and counting it as four
hundred is how a long tail gets mistaken for a working set.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tesseract.lib.jsonl_rolls import rewrite

logger = logging.getLogger(__name__)

#: A THREADING lock, not an asyncio one, and held inside `_append` rather than
#: around the `to_thread` call. Appends arrive from the event loop; the
#: retention sweep prunes from a worker thread with no loop of its own. One
#: lock both can take is what stops a row appended between the prune's read and
#: its rewrite from landing in neither file — the lost-write the scheduler and
#: approval ledgers each solved the same way, on the module that owns the file.
_LOCK = threading.Lock()


def usage_path() -> Path:
    """Resolved at call time, never at import — `log_dir` follows
    `TESSERACT_HOME`, and a module-level path would pin a test's writes to the
    operator's real log tree."""
    from tesseract.paths import log_dir

    return log_dir("usage") / "tools.jsonl"


async def record_tool_call(tool: str, session_id: str = "") -> None:
    """Append one row. Best-effort by construction: a ledger that can fail a
    turn is worse than no ledger, and this one exists to inform a decision
    nobody makes during a turn."""
    row = json.dumps(
        {
            "at_utc": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "session_id": session_id,
        },
        separators=(",", ":"),
    )
    try:
        await asyncio.to_thread(_append, usage_path(), row + "\n")
    except Exception:
        logger.debug("tool usage: could not record %s", tool, exc_info=True)


def _append(path: Path, line: str) -> None:
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)


def rank(path: Path | None = None) -> list[tuple[str, int, int]]:
    """`(tool, distinct_sessions, calls)`, most-used first.

    A malformed line is skipped rather than raised on. The file is appended to
    from a running system and read by a person deciding something; a half-line
    from a crash mid-write must not be the reason they cannot read it.
    """
    target = path or usage_path()
    if not target.exists():
        return []
    sessions: dict[str, set[str]] = defaultdict(set)
    calls: dict[str, int] = defaultdict(int)
    for line in target.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            tool = row["tool"]
        except Exception:
            continue
        sessions[tool].add(row.get("session_id") or "")
        calls[tool] += 1
    return sorted(
        ((t, len(sessions[t]), calls[t]) for t in calls),
        key=lambda r: (-r[1], -r[2], r[0]),
    )


def _row_time(row: dict) -> datetime | None:
    """`at_utc` as an aware datetime, or None if it will not parse."""
    try:
        stamped = datetime.fromisoformat(str(row["at_utc"]))
    except (KeyError, TypeError, ValueError):
        return None
    return stamped if stamped.tzinfo else stamped.replace(tzinfo=timezone.utc)


def rollup(
    days: int,
    roster: Iterable[str] = (),
    *,
    now: datetime | None = None,
    path: Path | None = None,
) -> list[dict[str, object]]:
    """Usage over the last `days`, one entry per tool in `roster`.

    **Every tool in the roster appears, including the ones with nothing.** A
    zero row is the interesting half — it is a demotion candidate — and a
    reader that returns only the ledger's own keys cannot show one. The roster
    comes from the live registry, so a tool deleted from the code stops being
    reported even though its old rows are still on disk.

    Ranked by distinct sessions, then calls, then name. Never by raw calls:
    one loop calling `lane_read` four hundred times is one session's worth of
    evidence, and counting it as four hundred is how a long tail gets mistaken
    for a working set.

    A row whose timestamp will not parse is EXCLUDED — the opposite of what
    `prune_older_than` does with the same row, and deliberately. Here the
    risk is claiming a call happened inside a window that cannot be shown to
    contain it; there the risk is deleting something. Neither guesses; each
    fails toward the answer that cannot mislead.
    """
    target = path or usage_path()
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    sessions: dict[str, set[str]] = defaultdict(set)
    calls: dict[str, int] = defaultdict(int)

    if target.exists():
        for line in target.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                tool = row["tool"]
            except Exception:
                continue
            stamped = _row_time(row)
            if stamped is None or stamped < cutoff:
                continue
            sessions[tool].add(row.get("session_id") or "")
            calls[tool] += 1

    names = set(roster) | set(calls)
    return sorted(
        (
            {"tool": name, "sessions": len(sessions[name]), "calls": calls[name]}
            for name in names
        ),
        key=lambda r: (-r["sessions"], -r["calls"], r["tool"]),
    )


def prune_older_than(cutoff: datetime, path: Path | None = None) -> int:
    """Drop rows older than `cutoff`. Returns how many went.

    Held under the same lock the append takes, so a call recorded between the
    read and the replace cannot land in neither — the lost-write the scheduler
    and approval ledgers each solved this way, on the module that owns the file.

    A row whose timestamp will not parse is KEPT. An unreadable timestamp is
    not a licence to guess which side of the window it falls on, and the cost
    of guessing wrong here is a deleted row rather than an omitted one.

    Rewritten in place rather than archived: this is usage telemetry, not the
    evidence an investigation reads. Nothing here answers a forensic question,
    which is exactly why `may_delete` is True for it and False for the approval
    ledger.
    """
    target = path or usage_path()
    with _LOCK:
        if not target.is_file():
            return 0
        keep: list[str] = []
        removed = 0
        for line in target.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                stamped = _row_time(json.loads(line))
            except Exception:
                stamped = None
            if stamped is not None and stamped < cutoff:
                removed += 1
                continue
            keep.append(line)
        if removed:
            # `jsonl_rolls.rewrite`, not `write_text`. Both ledgers this
            # follows use it, and porting only the locking half left the
            # riskier one open: `write_text` truncates and THEN writes, so a
            # crash between the two loses not the pruned rows but the whole
            # ledger. Temp file + `os.replace` instead.
            rewrite(target, keep)
        return removed


__all__ = ["record_tool_call", "usage_path", "rank", "rollup", "prune_older_than"]
