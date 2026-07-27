from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

_DEFAULT_LOG_DIR = Path("tesseract/logs/schedule")
_LOG_FILENAME = "runs.jsonl"


def append_run_log(
    ctx: JobContext,
    result: JobResult,
    completed_at: datetime | None = None,
    log_dir: Path | None = None,
) -> Path:
    """Append one JSON line to runs.jsonl; create the directory on first write."""
    target_dir = log_dir if log_dir is not None else _DEFAULT_LOG_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / _LOG_FILENAME

    # File-log writers emit local-zone ISO (with offset) so the raw file
    # is readable at a glance — e.g. `2026-04-21T19:51:05.123+02:00` instead
    # of UTC `…T17:51:05.123+00:00`. Datetime comparisons on parsed-back
    # values (`load_last_runs`) stay correct across timezones because
    # `datetime.fromisoformat` preserves tzinfo.
    entry = {
        "job_name": result.job_name,
        "run_id": result.run_id,
        "fired_at": ctx.fired_at.astimezone().isoformat(),
        "completed_at": (completed_at or datetime.now(timezone.utc)).astimezone().isoformat(),
        "ok": result.ok,
        "detail": result.detail,
        "payload": result.payload,
        "duration_ms": result.duration_ms,
    }
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return target


def load_last_runs(log_dir: Path | None = None) -> dict[str, datetime]:
    """Scan runs.jsonl once and return {job_name: latest fired_at} (UTC).

    Used by the engine on startup to decide which jobs need a catch-up fire.
    We key on `fired_at` (not `completed_at`) because that's the tick time the
    cron schedule corresponds to. Malformed lines are skipped with a WARN.
    """
    target_dir = log_dir if log_dir is not None else _DEFAULT_LOG_DIR
    target = target_dir / _LOG_FILENAME
    latest: dict[str, datetime] = {}
    if not target.exists():
        return latest
    with target.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
                name = entry["job_name"]
                # Normalize to UTC: written entries are local-tz ISO, but the
                # engine treats `last_fired_at` as UTC tz-aware everywhere
                # (interval delta math, in-slot dedupe, runtime_state ISO).
                fired_at = datetime.fromisoformat(entry["fired_at"]).astimezone(timezone.utc)
            except (json.JSONDecodeError, KeyError, ValueError):
                log.warning("scheduler: skipping malformed runs.jsonl line %d", line_no)
                continue
            previous = latest.get(name)
            if previous is None or fired_at > previous:
                latest[name] = fired_at
    return latest
