"""Every closed task, with its verdict and who gave it.

A reader, not a store. `agenda_history` keeps one write-once row per finished
item and `verdicts` keeps the operator's keys; this joins them and derives the
verdict at read time, so a verdict given after the close never rewrites the
row that outlives the record.

Invariants, held together:
  1. A verdict comes from something other than the actor. A close on the
     assistant's own word (`verification_by == "model"`, or nothing written)
     reads `unverified`, whatever status it closed on.
  2. The operator outranks the check: `good` or `bad` from them is the
     verdict even where a check ran, because a passing check says the project
     still builds and they say whether the thing they wanted happened.
  3. `unused` is the operator's own column, pressed or read from silence, and
     never overturns a check. `pending` is a task still inside the window.
     `not_offered` is a task that stopped without finishing: no key was ever
     offered on it, so its silence is not a judgement of anything.
  4. `unverified`, `unused`, `pending` and `not_offered` are counted in every
     total `tally` returns. A reader that dropped them would report on the
     easy cases.
  5. Every closed task is returned, however it ended.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from tesseract.orchestrator.autonomy import verdicts
from tesseract.orchestrator.autonomy.agenda_history import closed_since

Verdict = Literal["good", "bad", "passed", "failed", "unverified"]

VERDICTS: tuple[Verdict, ...] = ("good", "bad", "passed", "failed", "unverified")

Operator = Literal["good", "bad", "unused", "pending", "not_offered"]

OPERATOR: tuple[Operator, ...] = ("good", "bad", "unused", "pending", "not_offered")


def verdict_of(row: dict[str, Any], operator: str = "pending") -> tuple[Verdict, str]:
    """The verdict on one history row and its source, `""` when nothing judged it."""
    if operator in ("good", "bad"):
        return operator, "operator"  # type: ignore[return-value]
    if row.get("verification_by") == "gate":
        if row.get("status") == "done":
            return "passed", "gate"
        if row.get("status") == "failed":
            return "failed", "gate"
    return "unverified", ""


def operator_of(
    row: dict[str, Any],
    key: dict[str, Any] | None,
    *,
    now: datetime,
    silence_hours: float,
) -> Operator:
    """What the operator said, or `unused` once the window has passed in silence."""
    if row.get("status") not in verdicts.JUDGED_STATUSES:
        return "not_offered"
    if key is not None:
        return key["verdict"]
    try:
        closed = datetime.fromisoformat(str(row.get("closed_at") or ""))
    except ValueError:
        return "pending"
    if closed.tzinfo is None:
        closed = closed.replace(tzinfo=timezone.utc)
    return "unused" if now - closed >= timedelta(hours=silence_hours) else "pending"


def closed_tasks(
    watermark: datetime | None,
    *,
    now: datetime | None = None,
    silence_hours: float | None = None,
) -> list[dict[str, Any]]:
    """Every task that closed after `watermark`, oldest first, with its verdict.

    Tasks only: autonomy's own items close on their own and were never asked
    for, so counting them would dilute the one question this answers.
    """
    keys = verdicts.latest()
    hours = verdicts.silence_hours() if silence_hours is None else silence_hours
    moment = now or datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for row in closed_since(watermark):
        if row.get("source") != "task":
            continue
        operator = operator_of(row, keys.get(str(row.get("id"))), now=moment, silence_hours=hours)
        verdict, source = verdict_of(row, operator)
        out.append({**row, "operator": operator, "verdict": verdict, "verdict_source": source})
    return out


def tally(rows: list[dict[str, Any]]) -> dict[str, dict[str, int] | int]:
    """How many rows carry each verdict and each operator answer, every value present."""
    verdict = Counter(str(row.get("verdict") or "unverified") for row in rows)
    operator = Counter(str(row.get("operator") or "pending") for row in rows)
    return {
        "verdict": {v: verdict.get(v, 0) for v in VERDICTS},
        "operator": {o: operator.get(o, 0) for o in OPERATOR},
        "total": len(rows),
    }


__all__ = [
    "OPERATOR",
    "Operator",
    "VERDICTS",
    "Verdict",
    "closed_tasks",
    "operator_of",
    "tally",
    "verdict_of",
]
