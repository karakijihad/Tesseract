"""The operator's verdict on a finished task: good, bad or unused, one key.

Append-only `agenda/verdicts/YYYY-MM.jsonl`, bucketed by the month the key was
pressed. The last key for a task wins, so a wrong tap is corrected by pressing
again and nothing is ever rewritten.

Invariants, held together:
  1. One write function. Every surface that offers the key calls
     `record_verdict`; a surface with its own copy is how two answers drift.
  2. Only a finished task takes a verdict: its row must be in the history
     and it must have closed done or failed. Autonomy's own items are refused,
     because nobody asked for them, and so is a task that was cancelled,
     abandoned or superseded, because it produced no work to judge.
  3. The history row is never touched. The verdict is joined at read time.
  4. Silence is not written down. `unused` from silence is derived by the
     reader from `silence_hours`, so the setting reads the past consistently.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from tesseract.orchestrator.autonomy.agenda_history import closed_since
from tesseract.orchestrator.autonomy.paths import agenda_root

log = logging.getLogger(__name__)

_LOCK = threading.Lock()

#: Told once, after a key is actually written. Never on "invalid",
#: "unknown_task", "not_a_task", "never_finished" or "unwritable": nothing
#: happened on any of those, so nothing to announce. Mirror server boot
#: is the one caller; a listener exception is logged and swallowed here so
#: the key is saved either way.
RecordedListener = Callable[[str, str, str], None]
_recorded_listener: RecordedListener | None = None


def set_recorded_listener(fn: RecordedListener | None) -> None:
    """Wire (or clear) the sync callback fired after a successful write.

    Called with `(task_id, verdict, by)`. `record_verdict` may run on a
    worker thread (`asyncio.to_thread`), so the listener itself must be
    thread-safe; Mirror server boot's listener captures the running loop at
    registration time and schedules its broadcast with
    `loop.call_soon_threadsafe` for exactly that reason. Tests must reset
    this to `None` in teardown.
    """
    global _recorded_listener
    _recorded_listener = fn


OperatorVerdict = Literal["good", "bad", "unused"]

VALID_VERDICTS: tuple[OperatorVerdict, ...] = ("good", "bad", "unused")

Recorded = Literal[
    "recorded", "invalid", "unknown_task", "not_a_task", "never_finished", "unwritable"
]

#: The statuses a key is offered on: the two a task closes with when work ran.
JUDGED_STATUSES = frozenset({"done", "failed"})


def verdicts_dir() -> Path:
    """`<TESSERACT_HOME>/agenda/verdicts/`."""
    return agenda_root() / "verdicts"


def silence_hours() -> float:
    """`agenda.yaml::verdict.silence_hours`, or a raised `KeyError`."""
    import yaml

    from tesseract.paths import config_dir

    path = config_dir() / "agenda.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    block = raw.get("verdict")
    if not isinstance(block, dict) or "silence_hours" not in block:
        raise KeyError(f"{path}: verdict.silence_hours is missing")
    return float(block["silence_hours"])


def _history_row(task_id: str) -> dict[str, Any] | None:
    for row in closed_since(None):
        if row.get("id") == task_id:
            return row
    return None


def record_verdict(
    task_id: str, verdict: str, *, by: str, now: datetime | None = None
) -> Recorded:
    """Append one key for a finished task. Says what happened; never raises."""
    if verdict not in VALID_VERDICTS:
        return "invalid"
    row = _history_row(task_id)
    if row is None:
        return "unknown_task"
    if row.get("source") != "task":
        return "not_a_task"
    if row.get("status") not in JUDGED_STATUSES:
        return "never_finished"
    stamped = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    line = json.dumps(
        {"at": stamped.isoformat(), "task": task_id, "verdict": verdict, "by": by},
        separators=(",", ":"),
    )
    path = verdicts_dir() / f"{stamped:%Y-%m}.jsonl"
    try:
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError:
        log.exception("verdicts: could not record %s for %s", verdict, task_id)
        return "unwritable"
    if _recorded_listener is not None:
        try:
            _recorded_listener(task_id, verdict, by)
        except Exception:
            log.exception(
                "verdicts: recorded-listener raised for %s on %s; the key is saved either way",
                verdict, task_id,
            )
    return "recorded"


def latest() -> dict[str, dict[str, Any]]:
    """The last key pressed for each task, keyed by task id."""
    found: dict[str, dict[str, Any]] = {}
    root = verdicts_dir()
    if not root.is_dir():
        return found
    for path in sorted(root.glob("*.jsonl")):
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:
            log.exception("verdicts: unreadable file at %s", path)
            continue
        for text in body.splitlines():
            try:
                row = json.loads(text)
            except ValueError:
                continue
            if (
                isinstance(row, dict)
                and isinstance(row.get("task"), str)
                and row.get("verdict") in VALID_VERDICTS
            ):
                found[row["task"]] = row
    return found


__all__ = [
    "JUDGED_STATUSES",
    "OperatorVerdict",
    "Recorded",
    "RecordedListener",
    "VALID_VERDICTS",
    "latest",
    "record_verdict",
    "set_recorded_listener",
    "silence_hours",
    "verdicts_dir",
]
