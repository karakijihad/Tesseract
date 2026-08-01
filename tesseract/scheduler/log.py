from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from tesseract.paths import TESSERACT_HOME
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

_LOG_FILENAME = "runs.jsonl"


def default_log_dir() -> Path:
    """`<TESSERACT_HOME>/logs/schedule` — resolved at call time.

    This used to be a module-level `Path("tesseract/logs/schedule")`: a
    RELATIVE path, so the run log landed wherever the process happened to be
    started from. In a dev checkout (cwd = repo root) that coincides with the
    right place, which is why it went unnoticed; in a packaged install the
    supervisor is spawned with no `current_dir` and inherits the shortcut's
    cwd, so `runs.jsonl` was written outside `TESSERACT_HOME` — or not at all.

    Two failures followed, both silent. `load_last_runs` reads this same
    location, so `_compute_catchup` saw no prior run for any job and skipped
    every missed tick. And `recovery/manager.py` + `conscience/drift.py` both
    look under `home / "logs" / "schedule"`, so they were reading a file the
    writer never created.

    Call-time (not import-time) so a `TESSERACT_HOME` monkeypatch in tests is
    honored without re-importing this module — the canonical pattern from
    `kernel/workspace_changes.py::workspace_events_dir`.
    """
    return Path(os.environ.get("TESSERACT_HOME") or TESSERACT_HOME) / "logs" / "schedule"


def append_run_log(
    ctx: JobContext,
    result: JobResult,
    completed_at: datetime | None = None,
    log_dir: Path | None = None,
) -> Path:
    """Append one JSON line to runs.jsonl; create the directory on first write."""
    target_dir = log_dir if log_dir is not None else default_log_dir()
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
    target_dir = log_dir if log_dir is not None else default_log_dir()
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
