"""Rule-based drift detector — scrapes existing log files, emits signal report.

No LLM, no live instrumentation. Three signals for the MVP rewire:

  * circuit_breakers_not_recovering — how many breakers under
    `runtime/logs/circuit-breakers/` are open AND will not try again on
    their own. It counted every open one, which rated a breaker
    mid-cooldown (normal, self-healing) the same as one whose probe is
    off, and rated one permanently-off capability as a third of a
    problem. Open is not the fault; not coming back is.
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

from tesseract.lib.log_envelope import envelope

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

    @property
    def worst(self) -> SignalStatus:
        """The band this whole report sits in.

        The severity a reader used to work out for itself by counting the
        signals, which meant two readers agreed only by accident.
        """
        if self.summary["bad"]:
            return "bad"
        return "warn" if self.summary["warn"] else "ok"

    def to_json(self) -> dict:
        troubled = sorted(s.name for s in self.signals if s.status != "ok")
        return {
            **envelope(
                stream="conscience",
                # The signal vocabulary is ok/warn/bad and the envelope's is
                # info/warn/bad. Only the healthy end differs, and it differs
                # because "ok" is a judgement about a signal while "info" is
                # what a record is worth to whoever reads it.
                severity="info" if self.worst == "ok" else self.worst,
                subject=", ".join(troubled) or "every signal",
                summary=(
                    f"the drift check rates {len(troubled)} of "
                    f"{len(self.signals)} signals above ok"
                    if troubled
                    else f"the drift check rates all {len(self.signals)} signals ok"
                ),
                detail={"window_hours": self.window_hours},
                ts=self.timestamp,
            ),
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
            # The tally, under the name it describes. It was `summary` until
            # the envelope needed that name for the sentence, and a count of
            # signals per band was never a summary line. Readers take either.
            "counts": self.summary,
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
        _signal_circuit_breakers(breakers_dir, thresholds["circuit_breakers_not_recovering"]),
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
    """Breakers that are open and are not going to come back by themselves.

    Read through `breaker_report`, which is the one place that knows whether a
    probe is due: it combines the cooldown the trip recorded, the backoff for
    repeat trips and any live object holding the name. Counting open files here
    instead was both too loud and too quiet. Too loud, because a breaker
    mid-cooldown is the system working. Too quiet, because a capability that is
    off until someone acts read as one third of a problem.
    """
    stuck: list[str] = []
    try:
        from tesseract.context.circuit_breaker import breaker_report, is_recovering

        rows = breaker_report(breakers_dir) if breakers_dir.exists() else []
    except Exception:  # noqa: BLE001 — one signal must not end the report
        rows = []
    for row in rows:
        if row.get("tripped") and not is_recovering(row):
            stuck.append(str(row.get("name") or ""))
    stuck.sort()
    return SignalResult(
        name="circuit_breakers_not_recovering",
        value=float(len(stuck)),
        status=_classify_high_bad(len(stuck), threshold),
        warn=threshold["warn"],
        bad=threshold["bad"],
        detail=", ".join(stuck),
    )


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
