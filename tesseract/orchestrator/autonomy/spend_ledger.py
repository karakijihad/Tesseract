"""What autonomy actually spent today, on disk.

``AgendaStore.today_spend()`` sums the live items, and an item is archived the
instant it terminates — so the total it returned was in-flight spend only, and
the ``daily_caps.tokens`` / ``daily_caps.seconds`` ceilings reading it could
have tripped only if a whole day's budget were mid-run at one instant. Neither
ever fired. This counts what completed, so the caps mean what the config says.

One file per UTC day, keyed by worker id so a record finalized twice is counted
once. Written by the autonomy kernel, which runs in a single process; the
atomic replace is what protects the file from a crash mid-write, not from a
second writer.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from tesseract.orchestrator.autonomy.paths import agenda_root

log = logging.getLogger(__name__)


def spend_dir() -> Path:
    """``<TESSERACT_HOME>/agenda/spend/`` — one JSON file per UTC day."""
    return agenda_root() / "spend"


def spend_path(day: date | None = None) -> Path:
    stamp = (day or datetime.now(timezone.utc).date()).isoformat()
    return spend_dir() / f"{stamp}.json"


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"tokens": 0, "seconds": 0, "workers": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.exception("autonomy spend: unreadable ledger %s — starting a fresh day", path)
        return {"tokens": 0, "seconds": 0, "workers": {}}
    if not isinstance(raw, dict):
        return {"tokens": 0, "seconds": 0, "workers": {}}
    raw.setdefault("tokens", 0)
    raw.setdefault("seconds", 0)
    if not isinstance(raw.get("workers"), dict):
        raw["workers"] = {}
    return raw


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def record_spend(
    worker_id: str,
    *,
    tokens: int,
    seconds: float,
    day: date | None = None,
) -> dict[str, int]:
    """Add one worker's spend to today's ledger and return the new totals.

    Idempotent per ``worker_id``: a re-finalized record replaces its own row
    rather than adding to the day again.
    """
    path = spend_path(day)
    ledger = _load(path)
    ledger["workers"][worker_id] = {
        "tokens": int(tokens),
        "seconds": int(seconds),
        "at": datetime.now(timezone.utc).isoformat(),
    }
    ledger["tokens"] = sum(int(w.get("tokens") or 0) for w in ledger["workers"].values())
    ledger["seconds"] = sum(int(w.get("seconds") or 0) for w in ledger["workers"].values())
    _atomic_write(path, ledger)
    return {"tokens": ledger["tokens"], "seconds": ledger["seconds"]}


def day_spend(day: date | None = None) -> dict[str, int]:
    """Totals for one UTC day. Zero for a day with no ledger file."""
    ledger = _load(spend_path(day))
    return {"tokens": int(ledger["tokens"]), "seconds": int(ledger["seconds"])}


__all__ = ["day_spend", "record_spend", "spend_dir", "spend_path"]
