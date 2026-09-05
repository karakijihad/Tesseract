"""One compact row per finished agenda item, kept for good.

The full record of an item is a few kilobytes and grows with every transition
and attempt; the fact that it existed, what it was for and how it ended is
about two hundred bytes. The full record ages out under `retention.yaml`, the
row does not, so a task finished two years ago can still be named, dated and
counted after its record is gone.

`agenda/history/YYYY-MM.jsonl`, bucketed by the month the item closed, the
same way the archive is. Written twice over, on purpose: at close by the store,
and again by the retention sweep before it deletes a record that has no row,
which is how items closed before this file existed get theirs. A row is never
written twice for one id in one month.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tesseract.orchestrator.autonomy.models import AgendaItem
from tesseract.orchestrator.autonomy.paths import agenda_root

log = logging.getLogger(__name__)

_LOCK = threading.Lock()


def history_dir() -> Path:
    """`<TESSERACT_HOME>/agenda/history/`."""
    return agenda_root() / "history"


def month_of(item: AgendaItem) -> str:
    """The bucket an item closed into: its last transition, in UTC."""
    return item.updated_at.astimezone(timezone.utc).strftime("%Y-%m")


def history_path(month: str) -> Path:
    return history_dir() / f"{month}.jsonl"


def row_for(item: AgendaItem) -> dict[str, Any]:
    """What survives the record: enough to name, date and count it."""
    closed = item.updated_at.astimezone(timezone.utc)
    created = item.created_at.astimezone(timezone.utc)
    return {
        "id": item.id,
        "source": item.source.value,
        "goal": item.goal,
        "status": item.status.value,
        "created_at": created.isoformat(),
        "closed_at": closed.isoformat(),
        "duration_s": round((closed - created).total_seconds(), 1),
        "tokens_spent": item.budget_tokens_spent,
        "seconds_spent": item.budget_seconds_spent,
        "turns": len(item.turn_ids),
        # Who wrote the evidence and which project's checks it was: a reader
        # deciding what to learn from needs both without opening the item.
        "verification_by": item.verification_by,
        "project_id": item.project_id,
        "schema_version": item.schema_version,
    }


def _ids_in(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    found: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            found.add(row["id"])
    return found


def record_closed(item: AgendaItem) -> bool:
    """Append the item's row unless one is there. Returns whether it wrote.

    Refuses a live item: a row says the item is finished, and writing one for
    work still open would be the record lying about the one thing it keeps.
    Never raises for a bad disk; the caller's write is the point and this is
    the receipt.
    """
    if not item.is_terminal():
        raise ValueError(f"agenda history: {item.id!r} is {item.status.value}, not finished")
    path = history_path(month_of(item))
    try:
        with _LOCK:
            if item.id in _ids_in(path):
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row_for(item), default=str) + "\n")
        return True
    except OSError:
        log.exception("agenda history: could not record %s", item.id)
        return False


def rows(month: str) -> list[dict[str, Any]]:
    """Every row in one month's file, in the order they were written."""
    path = history_path(month)
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def done_since(watermark: "datetime | None") -> list[dict[str, Any]]:
    """Every row that closed `done` after `watermark`, oldest first.

    `None` means everything on disk, which is what makes a first pass read
    the whole record. A row whose `closed_at` will not parse is not new: a
    reader may not act on evidence it could not read.
    """
    from datetime import datetime as _dt

    found: list[tuple[_dt, dict[str, Any]]] = []
    root = history_dir()
    if not root.is_dir():
        return []
    for path in sorted(root.glob("*.jsonl")):
        for row in rows(path.stem):
            if row.get("status") != "done":
                continue
            try:
                closed = _dt.fromisoformat(str(row.get("closed_at") or ""))
            except ValueError:
                continue
            if closed.tzinfo is None:
                closed = closed.replace(tzinfo=timezone.utc)
            if watermark is not None and closed <= watermark:
                continue
            found.append((closed, row))
    return [row for _, row in sorted(found, key=lambda pair: pair[0])]


__all__ = [
    "done_since",
    "history_dir",
    "history_path",
    "month_of",
    "record_closed",
    "row_for",
    "rows",
]
