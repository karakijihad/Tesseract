"""Every closed task, with its verdict and who gave it.

A reader, not a store. `agenda_history` keeps one write-once row per finished
item; this reads those rows and derives the verdict at read time, so a verdict
given after the close never rewrites the row that outlives the record.

Invariants, held together:
  1. A verdict comes from something other than the actor. A close on the
     assistant's own word (`verification_by == "model"`, or nothing written)
     reads `unverified`, whatever status it closed on.
  2. `unverified` is a terminal verdict of its own, never folded into a
     success or a failure, and counted in every total `tally` returns.
  3. Every closed task is returned, however it ended. A reader that filtered
     here would make "nothing went wrong" and "nothing was judged" one answer.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Literal

from tesseract.orchestrator.autonomy.agenda_history import closed_since

Verdict = Literal["passed", "failed", "unverified"]

VERDICTS: tuple[Verdict, ...] = ("passed", "failed", "unverified")


def verdict_of(row: dict[str, Any]) -> tuple[Verdict, str]:
    """The verdict on one history row and its source, `""` when nothing judged it."""
    if row.get("verification_by") == "gate":
        if row.get("status") == "done":
            return "passed", "gate"
        if row.get("status") == "failed":
            return "failed", "gate"
    return "unverified", ""


def closed_tasks(watermark: datetime | None) -> list[dict[str, Any]]:
    """Every task that closed after `watermark`, oldest first, with its verdict.

    Tasks only: autonomy's own items close on their own and were never asked
    for, so counting them would dilute the one question this answers.
    """
    out: list[dict[str, Any]] = []
    for row in closed_since(watermark):
        if row.get("source") != "task":
            continue
        verdict, source = verdict_of(row)
        out.append({**row, "verdict": verdict, "verdict_source": source})
    return out


def tally(rows: list[dict[str, Any]]) -> dict[str, int]:
    """How many rows carry each verdict, every verdict present, plus `total`."""
    counts = Counter(str(row.get("verdict") or "unverified") for row in rows)
    return {**{v: counts.get(v, 0) for v in VERDICTS}, "total": len(rows)}


__all__ = ["VERDICTS", "Verdict", "closed_tasks", "tally", "verdict_of"]
