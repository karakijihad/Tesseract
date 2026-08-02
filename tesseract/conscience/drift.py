"""Rule-based drift detector — scrapes existing log files, emits signal report.

No LLM, no live instrumentation. Three signals for the MVP rewire:

  * circuit_breaker_open_count — how many breakers under
    `runtime/logs/circuit-breakers/` currently have `tripped` as their
    most recent event.
  * scheduler_failure_rate — fraction of `runs.jsonl` entries in the
    configured window where `ok == False`.
  * scheduler_idle_hours — hours since the most recent `runs.jsonl`
    entry in the window; `bad` when no entries are found at all.

Thresholds come from `tesseract/config/conscience.yaml` via
`tesseract/conscience/config.py`. All comparisons are "higher is worse".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

SignalStatus = Literal["ok", "warn", "bad"]


@dataclass(frozen=True)
class SignalResult:
    name: str
    value: float
    status: SignalStatus
    warn: float
    bad: float
    detail: str = ""


@dataclass(frozen=True)
class DriftReport:
    timestamp: datetime
    window_hours: int
    signals: list[SignalResult]

    @property
    def summary(self) -> dict[str, int]:
        counts = {"ok": 0, "warn": 0, "bad": 0}
        for sig in self.signals:
            counts[sig.status] += 1
        return counts

    def to_json(self) -> dict:
        return {
            "timestamp": self.timestamp.astimezone(timezone.utc).isoformat(),
            "window_hours": self.window_hours,
            "signals": [
                {
                    "name": s.name,
                    "value": s.value,
                    "status": s.status,
                    "warn": s.warn,
                    "bad": s.bad,
                    "detail": s.detail,
                }
                for s in self.signals
            ],
            "summary": self.summary,
        }


def evaluate_drift(
    *,
    schedule_log_dir: Path,
    breakers_dir: Path,
    thresholds: dict[str, dict[str, float]],
    window_hours: int,
    now: datetime | None = None,
    enabled_job_count: int | None = None,
) -> DriftReport:
    """Return a typed DriftReport for the current window.

    `enabled_job_count` (optional): if provided and equals 0, the
    `scheduler_idle_hours` signal short-circuits to `ok` with
    detail="no_enabled_jobs" — the scheduler can't be "idle" if the
    operator has nothing scheduled. Omit (or pass `None`) to use the
    plain "higher is worse" classifier.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = now - timedelta(hours=window_hours)
    runs = _load_runs_in_window(schedule_log_dir, cutoff)
    signals = [
        _signal_circuit_breakers(breakers_dir, thresholds["circuit_breaker_open_count"]),
        _signal_failure_rate(runs, thresholds["scheduler_failure_rate"]),
        _signal_idle_hours(runs, now, thresholds["scheduler_idle_hours"], enabled_job_count),
    ]
    return DriftReport(timestamp=now, window_hours=window_hours, signals=signals)


def _load_runs_in_window(schedule_log_dir: Path, cutoff: datetime) -> list[dict]:
    path = schedule_log_dir / "runs.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
                fired_at = datetime.fromisoformat(entry["fired_at"]).astimezone(timezone.utc)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if fired_at < cutoff:
                continue
            entry["fired_at"] = fired_at
            out.append(entry)
    return out


def _signal_circuit_breakers(
    breakers_dir: Path, threshold: dict[str, float]
) -> SignalResult:
    names_open: list[str] = []
    if breakers_dir.exists():
        for log_file in sorted(breakers_dir.glob("*.jsonl")):
            if _is_breaker_open(log_file):
                names_open.append(log_file.stem)
    open_count = len(names_open)
    return SignalResult(
        name="circuit_breaker_open_count",
        value=float(open_count),
        status=_classify_high_bad(open_count, threshold),
        warn=threshold["warn"],
        bad=threshold["bad"],
        detail=", ".join(names_open),
    )


def _is_breaker_open(log_file: Path) -> bool:
    last_event: str | None = None
    with log_file.open("r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                evt = json.loads(raw)
            except json.JSONDecodeError:
                continue
            last_event = evt.get("event")
    return last_event == "tripped"


def _signal_failure_rate(
    runs: list[dict], threshold: dict[str, float]
) -> SignalResult:
    if not runs:
        return SignalResult(
            name="scheduler_failure_rate",
            value=0.0,
            status="ok",
            warn=threshold["warn"],
            bad=threshold["bad"],
            detail="no_runs_in_window",
        )
    failed = sum(1 for r in runs if not r.get("ok"))
    rate = failed / len(runs)
    return SignalResult(
        name="scheduler_failure_rate",
        value=round(rate, 4),
        status=_classify_high_bad(rate, threshold),
        warn=threshold["warn"],
        bad=threshold["bad"],
        detail=f"{failed}/{len(runs)} failed",
    )


def _signal_idle_hours(
    runs: list[dict],
    now: datetime,
    threshold: dict[str, float],
    enabled_job_count: int | None,
) -> SignalResult:
    # Carve-out: "idle" is only meaningful when something is supposed
    # to fire. If the operator has disabled every cron job, the
    # scheduler is correctly doing nothing — don't alarm.
    if enabled_job_count == 0:
        return SignalResult(
            name="scheduler_idle_hours",
            value=0.0,
            status="ok",
            warn=threshold["warn"],
            bad=threshold["bad"],
            detail="no_enabled_jobs",
        )
    if not runs:
        return SignalResult(
            name="scheduler_idle_hours",
            value=threshold["bad"],
            status="bad",
            warn=threshold["warn"],
            bad=threshold["bad"],
            detail="no_runs_in_window",
        )
    latest = max(r["fired_at"] for r in runs)
    idle = (now - latest).total_seconds() / 3600.0
    return SignalResult(
        name="scheduler_idle_hours",
        value=round(idle, 2),
        status=_classify_high_bad(idle, threshold),
        warn=threshold["warn"],
        bad=threshold["bad"],
        detail=f"last_run={latest.isoformat()}",
    )


def _classify_high_bad(value: float, threshold: dict[str, float]) -> SignalStatus:
    if value >= threshold["bad"]:
        return "bad"
    if value >= threshold["warn"]:
        return "warn"
    return "ok"
