from __future__ import annotations



import json

import logging

import os


import threading

from datetime import date, datetime, timezone

from pathlib import Path

from typing import Callable, Iterator



from tesseract.lib.jsonl_rolls import rewrite, row_time
from tesseract.paths import TESSERACT_HOME, log_dir

from tesseract.scheduler.types import JobContext, JobResult



log = logging.getLogger(__name__)



_LOG_FILENAME = "runs.jsonl"



# Held across an append and across the whole of `prune_older_than`. The pruner

# replaces the file with a list computed from a snapshot, so a run recorded

# between the read and the replace would be lost — and at 23:00 the anchor is

# not the only row firing. The lock lives here, with the file, which is why the

# pruning is a function on this module rather than something the sweep does by

# reaching for a private lock.

_LOCK = threading.Lock()





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

    return log_dir("schedule")





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

        # `ok` is kept for readers that predate the vocabulary; `outcome` is

        # what a health surface must count on, because it separates "found

        # nothing to do" and "refused to start" from both success and failure.

        "outcome": result.outcome.value if result.outcome else None,

        "outcome_reason": result.outcome_reason,

        "detail": result.detail,

        "payload": result.payload,

        "duration_ms": result.duration_ms,

        # Records written before this field existed have no trigger. Readers

        # must render its absence as unknown rather than assuming `scheduled` —

        # assuming is what turned a hand-fired job into a reported bug.

        "trigger_source": ctx.trigger_source,

    }

    with _LOCK, target.open("a", encoding="utf-8") as fh:

        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return target





def runs_path(log_dir: Path | None = None) -> Path:

    """`runs.jsonl`, wherever this install keeps it."""

    return (log_dir if log_dir is not None else default_log_dir()) / _LOG_FILENAME





def iter_runs(path: Path) -> Iterator[dict]:

    """Every well-formed row of a run log; malformed lines skipped with a WARN.



    One reader, because there have been four: this module, the recovery scan,

    the conscience drift check and the watchman's row source each opened this

    file and re-derived what a line is. Two of them had already drifted from

    the writer once and silently read nothing at all — see `default_log_dir`.

    What a row IS belongs beside the append that writes it.



    `fired_at` is left exactly as written, local-offset ISO. Normalising it is

    the caller's business because the callers do not agree: the engine wants

    UTC for delta math, the daily writer wants the local day.

    """

    if not path.exists():

        return

    with path.open("r", encoding="utf-8", errors="replace") as fh:

        for line_no, raw_line in enumerate(fh, start=1):

            raw_line = raw_line.strip()

            if not raw_line:

                continue

            try:

                row = json.loads(raw_line)

            except json.JSONDecodeError:

                log.warning("scheduler: skipping malformed runs.jsonl line %d", line_no)

                continue

            if isinstance(row, dict):

                yield row


def load_last_runs(log_dir: Path | None = None) -> dict[str, datetime]:

    """Scan runs.jsonl once and return {job_name: latest fired_at} (UTC).



    Used by the engine on startup to decide which jobs need a catch-up fire.

    We key on `fired_at` (not `completed_at`) because that's the tick time the

    cron schedule corresponds to. Malformed lines are skipped with a WARN.

    """

    latest: dict[str, datetime] = {}

    for entry in iter_runs(runs_path(log_dir)):

        try:

            name = entry["job_name"]

            # Normalize to UTC: written entries are local-tz ISO, but the

            # engine treats `last_fired_at` as UTC tz-aware everywhere

            # (interval delta math, in-slot dedupe, runtime_state ISO).

            fired_at = datetime.fromisoformat(entry["fired_at"]).astimezone(timezone.utc)

        except (KeyError, TypeError, ValueError):

            log.warning("scheduler: run log row carries no usable job_name/fired_at")

            continue

        previous = latest.get(name)

        if previous is None or fired_at > previous:

            latest[name] = fired_at

    return latest



def prune_older_than(
    cutoff: datetime,
    *,
    summarised: Callable[[date], bool],
    archive: bool = False,
    log_dir: Path | None = None,
) -> tuple[int, int]:
    """Drop rows older than `cutoff` whose DAY has already been summarised.

    Returns `(retired, held)` — how many rows went, and how many were old
    enough to go but had no rollup to go behind.

    **A row is only removed once something else has said what it meant.**
    `daily_writer` aggregates each day's runs into a `Daily rollup <date>`
    entry in the daily log layer every night, and `summarised` is how this asks
    whether that happened. A day it did not is KEPT and counted, because ageing
    a log and losing the only record of a week are the same operation performed
    with different luck.

    A row whose `fired_at` will not parse is kept too — an unreadable timestamp
    is not a licence to guess which side of the window it falls on.

    `archive=True` moves rows into `runs-archive/runs-YYYY-MM.jsonl` instead of
    dropping them, written BEFORE the live file is rewritten: an interruption
    between the two duplicates a row and cannot lose one.
    """
    target_dir = log_dir if log_dir is not None else default_log_dir()
    target = target_dir / _LOG_FILENAME
    with _LOCK:
        if not target.is_file():
            return (0, 0)
        keep: list[str] = []
        retire: dict[str, list[str]] = {}
        checked: dict[date, bool] = {}
        held = 0
        for line in target.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            stamped = row_time(line, "fired_at")
            if stamped is None or stamped >= cutoff:
                keep.append(line)
                continue
            day = stamped.astimezone().date()
            if day not in checked:
                # Once per DATE, not once per row. `summarised` reads a whole
                # session log to answer, and a day holds a few hundred runs —
                # so the unmemoised version read the same file a few hundred
                # times while holding the lock every other writer waits on.
                checked[day] = summarised(day)
            if not checked[day]:
                held += 1
                keep.append(line)
                continue
            retire.setdefault(stamped.strftime("%Y-%m"), []).append(line)
        if not retire:
            return (0, held)

        retired = sum(len(rows) for rows in retire.values())
        if archive:
            archive_dir = target_dir / "runs-archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            for month, rows in sorted(retire.items()):
                with (archive_dir / f"runs-{month}.jsonl").open(
                    "a", encoding="utf-8"
                ) as fh:
                    fh.write("\n".join(rows) + "\n")
        rewrite(target, keep)
        return (retired, held)
