"""When each agent card was last actually used.

Three of the shipped cards are referenced from nowhere in source, and finding
that out meant reading nineteen files. Nothing anywhere answers *"has this ever
run?"* — the cost ledger bills by ROLE, and most cards share `agents_default`,
so a card that has never been invoked in its life is indistinguishable from the
busiest one.

That is the same defect the schedule tracker exists for, one file over: the
schedule's version is *declared but not firing*, and this one is **declared but
never invoked**. It is what makes the roster's judgement possible — keep,
rewrite or delete — without a reading pass over the tree.

**Recorded where a card is turned into a call**, which is four places, not one:
the sub-session builder every `invoke_agent` and `agent_ask` goes through, the
brief's digesters, and the two memory jobs that load a card to build their own
prompt. There is no funnel below them — `load_agent` is also how a route lists
a card for the Managed system room, and counting a page render as an
invocation would
make the column mean nothing. So the writers are named here, in one module, and
a fifth site is a one-line call rather than a new mechanism.

Append-only, never raises: a telemetry write may not fail the run it describes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LOG_FILENAME = "invocations.jsonl"

# Rows older than this are still on disk and still readable; the roster simply
# stops counting them, so "17 times" means "17 times lately" rather than a
# number that only grows. The retention table is what eventually trims the
# file — this is the reading window, not a deletion policy.
COUNT_WINDOW_DAYS = 30


def invocations_path() -> Path:
    from tesseract.paths import log_dir

    return log_dir("agents") / LOG_FILENAME


@dataclass(frozen=True)
class Invocation:
    """The last time one card ran, and how often it has lately."""

    name: str
    last_at: datetime
    count: int
    via: str


def record(name: str, *, via: str) -> None:
    """Append one row: this card was used to build a call, just now.

    `via` names the caller — `invoke_agent`, `brief_render`, `vault_lint`,
    `vault_librarian` — so a card that only ever runs as part of one document
    reads differently from one the assistant reaches for.

    There is deliberately no outcome field. Whether the call that followed
    came back is the cost ledger's and the run log's answer, and a second
    half-kept copy of it here would be a column that lies the first time a
    caller forgets to update it. This file answers one question: was this card
    ever reached, and when.

    Never raises.
    """
    if not name:
        return
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "name": name,
        "via": via,
    }
    try:
        path = invocations_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(row, sort_keys=True))
            fh.write("\n")
    except Exception:  # noqa: BLE001 — telemetry never breaks the run
        logger.debug("agent invocations: write failed for %s", name, exc_info=True)


def _rows() -> list[dict[str, Any]]:
    path = invocations_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        logger.warning("agent invocations: unreadable at %s", path)
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("name"):
            out.append(row)
    return out


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def last_invocations(*, now: datetime | None = None) -> dict[str, Invocation]:
    """Per card name, its newest row and how many rows fall inside the window.

    A name absent from the result has never been invoked — which is the answer
    the roster is for, and the reason this returns a mapping rather than a
    list: absence has to be readable as a state, not inferred from a gap.
    """
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=COUNT_WINDOW_DAYS)
    latest: dict[str, Invocation] = {}
    for row in _rows():
        moment = _parse(row.get("ts"))
        if moment is None:
            continue
        name = str(row["name"])
        seen = latest.get(name)
        count = (seen.count if seen else 0) + (1 if moment >= cutoff else 0)
        if seen is None or moment > seen.last_at:
            latest[name] = Invocation(
                name=name,
                last_at=moment,
                count=count,
                via=str(row.get("via") or ""),
            )
        else:
            latest[name] = Invocation(
                name=seen.name,
                last_at=seen.last_at,
                count=count,
                via=seen.via,
            )
    return latest


__all__ = [
    "COUNT_WINDOW_DAYS",
    "Invocation",
    "invocations_path",
    "last_invocations",
    "record",
]
