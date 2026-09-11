"""Where a receipt lives once a turn has produced one.

Its own append log rather than a field on the step, and the reason is measured
rather than stylistic: `StageRow` cannot carry a payload. AR-17 measured it
from the other side, where a job running as a pipeline stage could leave only
`reason` reading `promoted=3` because there was nowhere else to put anything.
`RunManifest` already splits that way for everything larger than a reason, and
this is the same split.

**The key is `(turn id, stage)` and nothing new is written down to hold it.**
Both halves already exist on the records: the turn's `run_id` and the row's
`stage`, which carries its ordinal precisely so a turn calling one tool twice
has two rows. Adding a `receipt_key` field to `StageRow` would have been a
second spelling of a join both records can already make, and a second spelling
is a thing that can disagree with itself. What a step that OWED a receipt and
produced none leaves behind is not a key either: it is its own
`RunOutcome.UNVERIFIED`, on the row, durable without this file existing at all.

One file per calendar day, in the operator's timezone, matching how
`runtime/turns/` names its directories. A day is what a reader asks for, so a
day is what the reader opens.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timezone
from pathlib import Path

from tesseract.kernel.tools.receipt import Receipt
from tesseract.lib.clock import to_local
from tesseract.paths import runtime_dir

log = logging.getLogger(__name__)

#: A THREADING lock, not an asyncio one, for the reason `tool_usage` documents:
#: appends arrive from the event loop and a sweep prunes from a worker thread
#: with no loop of its own. One lock both can take is what stops a row appended
#: between a prune's read and its rewrite from landing in neither file.
_LOCK = threading.Lock()


def receipts_root() -> Path:
    """`runtime/receipts/`. Resolved at call time, never at import.

    `runtime_dir` follows `TESSERACT_HOME`, and a module-level path would pin a
    test's writes to the operator's real tree. That is a hard rule here and it
    is the one every logs-writer has broken at least once.
    """
    return runtime_dir() / "receipts"


def day_path(on: date) -> Path:
    """The file holding one day's receipts."""
    return receipts_root() / f"{on.isoformat()}.jsonl"


def record(
    *,
    turn_id: str,
    stage: str,
    tool: str,
    receipt: Receipt,
    now: datetime | None = None,
) -> None:
    """Append one receipt. Best effort, and never raises into a turn.

    Same rule the turn recorder and memory writes follow: the work is the point
    and the record is best effort. A turn that failed because its receipt could
    not be written would have traded the thing being recorded for the record of
    it.

    A receipt pointing at nothing is not written. `Receipt.nothing()` exists to
    tell the STEP that the call had no effect, which the step's own outcome
    then carries; a log of things that did not happen has no reader.
    """
    if not receipt.points_at_something:
        return
    stamped = now or datetime.now(timezone.utc)
    row = json.dumps(
        {
            "at_utc": stamped.isoformat(),
            "turn": turn_id,
            "stage": stage,
            "tool": tool,
            **receipt.to_dict(),
        },
        separators=(",", ":"),
    )
    try:
        _append(day_path(to_local(stamped).date()), row + "\n")
    except Exception:  # noqa: BLE001 - a record must not cost the work
        log.warning("receipts: could not record %s for %s", receipt.kind, tool,
                    exc_info=True)


def _append(path: Path, line: str) -> None:
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)


def read_day(on: date) -> dict[tuple[str, str], Receipt]:
    """One day's receipts, keyed by `(turn id, stage)`.

    The shape the join wants, built once here rather than by every caller. A
    line that cannot be parsed is skipped and logged: this is read to render a
    panel, and one bad line must not cost the day the rest of its rows.
    """
    found: dict[tuple[str, str], Receipt] = {}
    path = day_path(on)
    if not path.is_file():
        return found
    try:
        body = path.read_text(encoding="utf-8")
    except OSError:
        log.exception("receipts: unreadable day file at %s", path)
        return found
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            found[(str(raw["turn"]), str(raw["stage"]))] = Receipt.from_dict(raw)
        except (ValueError, KeyError, TypeError):
            log.warning("receipts: skipping an unreadable line in %s", path)
    return found


__all__ = ["day_path", "read_day", "receipts_root", "record"]
