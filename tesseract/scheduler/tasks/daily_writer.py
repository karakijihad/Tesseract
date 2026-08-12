"""DailyWriterJob — Layer 1 of the four-layer daily rollup.

Reads yesterday's entries from `tesseract/logs/schedule/runs.jsonl`,
aggregates per-job counts and latency, appends a `[scheduler]` JSONL
entry to `tesseract/logs/sessions/YYYY-MM-DD.jsonl`. Deterministic (no
LLM). Routed to the logs stream (M1 of memory-retune) so the librarian
never promotes scheduler rollups into `reference/`.

"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from tesseract.paths import TESSERACT_HOME
from tesseract.memory.log_notes import (
    _resolve_log_dir,
    append_log_entry,
    resolve_runtime_subdir,
)
from tesseract.scheduler import log as scheduler_log
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


class DailyWriterJob(BaseJob):
    async def run(self, ctx: JobContext) -> JobResult:
        try:
            target_date = (ctx.fired_at - timedelta(days=1)).date()
            # Off the loop: runs.jsonl accumulates one row per job run for
            # the life of the install and is read whole every night.
            rows = await asyncio.to_thread(
                _load_rows_for, target_date, _resolve_schedule_log_dir(ctx)
            )
            aggregates = _aggregate(rows)
            body = _build_body(target_date, aggregates, generated_at=ctx.fired_at)
            header = f"## [scheduler] Daily rollup {target_date.isoformat()}"
            log_dir = _resolve_log_dir(ctx.app, TESSERACT_HOME)
            # Probe picks a fragment that survives the `## [type] ` split in
            # JSONL encoding — the literal header never appears in the file.
            wrote = append_log_entry(
                header=header,
                body=body,
                log_dir=log_dir,
                date=datetime.combine(target_date, time(0, 0), tzinfo=timezone.utc),
                idempotency_probe=f"Daily rollup {target_date.isoformat()}",
            )
            total_runs = sum(a["runs"] for a in aggregates.values())
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail=f"jobs={len(aggregates)} runs={total_runs}",
                payload={
                    "jobs": len(aggregates),
                    "runs": total_runs,
                    "skipped": not wrote,
                    "target_date": target_date.isoformat(),
                },
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("daily_writer crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
            )


def _resolve_schedule_log_dir(ctx: JobContext):
    """Return the directory that holds `runs.jsonl`.

    Priority:
      1. `ctx.log_dir` (engine-scoped — set by `SchedulerEngine._run_job`).
      2. `app["memory_bundle"].store.store_dir.parent / "logs" / "schedule"`
         (shared `resolve_runtime_subdir` helper) so tests that drop the
         engine can still pin reads to a tmp tree.
      3. `scheduler_log.default_log_dir()` (`<TESSERACT_HOME>/logs/schedule`).
    """
    if ctx.log_dir is not None:
        return ctx.log_dir
    app = ctx.app
    if app is None or not hasattr(app, "get"):
        return scheduler_log.default_log_dir()
    bundle = app.get("memory_bundle")
    store = getattr(bundle, "store", None) if bundle is not None else None
    if getattr(store, "store_dir", None) is None:
        return scheduler_log.default_log_dir()
    # Shared helper performs the store_dir.parent / *parts join.
    return resolve_runtime_subdir(
        app, "logs", "schedule", fallback_root=scheduler_log.default_log_dir().parent,
    )


def _load_rows_for(target: date, log_dir=None) -> list[dict]:
    base = log_dir if log_dir is not None else scheduler_log.default_log_dir()
    path = base / scheduler_log._LOG_FILENAME
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
                fired_at = datetime.fromisoformat(entry["fired_at"])
            except (json.JSONDecodeError, KeyError, ValueError):
                log.warning("daily_writer: skipping malformed runs.jsonl line %d", line_no)
                continue
            if fired_at.date() != target:
                continue
            rows.append(entry)
    return rows


def _aggregate(rows: list[dict]) -> dict[str, dict]:
    buckets: dict[str, dict] = {}
    for r in rows:
        name = r.get("job_name", "")
        if not name:
            continue
        b = buckets.setdefault(name, {"runs": 0, "ok": 0, "failed": 0, "total_ms": 0.0})
        b["runs"] += 1
        if r.get("ok"):
            b["ok"] += 1
        else:
            b["failed"] += 1
        try:
            b["total_ms"] += float(r.get("duration_ms") or 0.0)
        except (TypeError, ValueError):
            pass
    for b in buckets.values():
        b["avg_ms"] = int(round(b["total_ms"] / b["runs"])) if b["runs"] else 0
    return buckets


def _build_body(target: date, aggregates: dict[str, dict], *, generated_at: datetime) -> str:
    stamp = generated_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    header_line = f"Cron rollup for {target.isoformat()} (generated by scheduler at {stamp})."
    if not aggregates:
        return f"{header_line}\n\nNo scheduled runs on {target.isoformat()}."
    lines = [header_line, "", "| Job | Runs | OK | Failed | Avg ms |", "|-----|------|----|--------|--------|"]
    for name in sorted(aggregates):
        b = aggregates[name]
        lines.append(f"| {name} | {b['runs']} | {b['ok']} | {b['failed']} | {b['avg_ms']} |")
    return "\n".join(lines)
