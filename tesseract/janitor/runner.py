"""Sweep orchestrator — the one entry all three call sites share.

The four sweeps are independent, so they run concurrently on a thread
pool (parallel-by-default); one sweep failing is recorded in
`SweepReport.errors` and never aborts the others. Every sweep appends a
JSONL row to `runtime/logs/janitor/sweeps.jsonl`."""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .archives import sweep_archives
from .config import JanitorConfig, load_janitor_config
from .models import Finding, SweepReport
from .processes import sweep_processes
from .scratch import sweep_scratch
from .sessions import sweep_sessions

log = logging.getLogger(__name__)

_SWEEPS = (
    ("processes", sweep_processes),
    ("scratch", sweep_scratch),
    ("sessions", sweep_sessions),
    ("archives", sweep_archives),
)


def _report_path() -> Path:
    from tesseract.paths import TESSERACT_HOME, log_dir

    env = os.environ.get("TESSERACT_HOME")
    home = Path(env).resolve() if env else TESSERACT_HOME
    return log_dir("janitor") / "sweeps.jsonl"


def _write_report(report: SweepReport) -> None:
    path = _report_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "started_at_utc": report.started_at_utc,
            "finished_at_utc": report.finished_at_utc,
            "dry_run": report.dry_run,
            "summary": report.summary(),
            "findings": [asdict(f) for f in report.findings],
            "errors": list(report.errors),
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("janitor: could not write sweep report")


def run_sweep(
    cfg: JanitorConfig | None = None, *, dry_run: bool = False
) -> SweepReport:
    cfg = cfg or load_janitor_config()
    started = datetime.now(timezone.utc).isoformat()
    findings: list[Finding] = []
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=len(_SWEEPS)) as pool:
        futures = {
            name: pool.submit(fn, cfg, dry_run=dry_run) for name, fn in _SWEEPS
        }
        for name, future in futures.items():
            try:
                findings.extend(future.result())
            except Exception as exc:  # noqa: BLE001 — isolate sweep failures
                errors.append(f"{name}: {exc}")
                log.exception("janitor: %s sweep failed", name)

    report = SweepReport(
        started_at_utc=started,
        finished_at_utc=datetime.now(timezone.utc).isoformat(),
        dry_run=dry_run,
        findings=tuple(findings),
        errors=tuple(errors),
    )
    _write_report(report)
    return report
