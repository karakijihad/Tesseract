"""``OperatorNudgeJob`` — periodic "still here, all is good" toast.

2026-05-19. Built after a chat-channel confabulation incident where the assistant
claimed "Done. Every 15 minutes I'll fire a toast" without invoking any
tool. This job is the real path for that promise: schedule it via
``schedule_create`` at any cadence; it composes a deterministic status
summary from current runtime state and ships through the AU-10
``OutboundNotifier`` under the new ``operator_nudge`` category.

Composition is purely rule-based — no LLM call on every tick. The body
folds three signals:

* worst band from the latest ``conscience/drift-*.jsonl`` entry
  (``ok`` / ``warn`` / ``bad``),
* active agenda count + count of recently-failed linked workers,
* governor-paused source count + crash-storm-latched flag.

The category sits OUTSIDE :data:`EXEMPT_CATEGORIES`, so it respects the
per-channel rate cap and the operator's runtime mutes. That's the right
default for a chatty toast — the operator can mute it from
NotificationsPane without affecting load-bearing alerts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

from tesseract.paths import TESSERACT_HOME, home_logs_root, runtime_dir
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


def _logs_dir() -> Path:
    import os
    override = os.environ.get("TESSERACT_HOME")
    base = Path(override).resolve() if override else TESSERACT_HOME
    return home_logs_root()


@dataclass(frozen=True)
class _StatusSnapshot:
    worst_band: str  # ok / warn / bad / unknown
    active_agenda: int
    failed_workers_recent: int
    paused_sources: int
    crash_storm_latched: bool


def _read_worst_band(conscience_dir: Path) -> str:
    """Latest band from the most recent drift JSONL. Returns ``unknown``
    when no file exists (cold start) or the file is unreadable."""
    if not conscience_dir.exists():
        return "unknown"
    candidates = sorted(
        conscience_dir.glob("drift-*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return "unknown"
    try:
        # Read last non-empty line of the most recent file.
        lines = candidates[0].read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            summary = record.get("summary") or {}
            if summary.get("bad", 0) > 0:
                return "bad"
            if summary.get("warn", 0) > 0:
                return "warn"
            return "ok"
    except (OSError, json.JSONDecodeError, ValueError):
        log.debug("operator_nudge: latest drift JSONL unreadable", exc_info=True)
    return "unknown"


def _count_active_agenda() -> int:
    """Walk ``<HOME>/agenda/active/*.json`` and return the count.
    Treats any read failure as 0 — the nudge is best-effort.
    """
    import os
    override = os.environ.get("TESSERACT_HOME")
    base = Path(override).resolve() if override else TESSERACT_HOME
    active = base / "agenda" / "active"
    if not active.exists():
        return 0
    try:
        return sum(1 for _ in active.glob("*.json"))
    except OSError:
        return 0


def _count_recent_failed_workers(window_minutes: int = 60) -> int:
    """Count worker records in ``workers/active/`` whose status is
    ``failed`` and were updated within the window. Avoids walking the
    archive — the nudge is about *recent* health, not history.
    """
    import os
    override = os.environ.get("TESSERACT_HOME")
    base = Path(override).resolve() if override else TESSERACT_HOME
    active = base / "workers" / "active"
    if not active.exists():
        return 0
    cutoff = time.time() - window_minutes * 60
    count = 0
    try:
        for worker_dir in active.iterdir():
            record_path = worker_dir / "record.json"
            if not record_path.exists():
                continue
            try:
                if record_path.stat().st_mtime < cutoff:
                    continue
                raw = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if str(raw.get("status") or "").lower() == "failed":
                count += 1
    except OSError:
        return 0
    return count


def _count_paused_sources() -> int:
    """Read ``<HOME>/agenda/source-pauses.json`` (PauseStore). 0 when
    absent or unreadable.
    """
    import os
    override = os.environ.get("TESSERACT_HOME")
    base = Path(override).resolve() if override else TESSERACT_HOME
    path = base / "agenda" / "source-pauses.json"
    if not path.exists():
        return 0
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if isinstance(raw, dict):
        return len(raw)
    if isinstance(raw, list):
        return len(raw)
    return 0


def _crash_storm_latched() -> bool:
    import os
    override = os.environ.get("TESSERACT_HOME")
    base = Path(override).resolve() if override else TESSERACT_HOME
    return (runtime_dir() / "crash_storm.json").exists()


def _capture_snapshot() -> _StatusSnapshot:
    conscience_dir = _logs_dir() / "conscience"
    return _StatusSnapshot(
        worst_band=_read_worst_band(conscience_dir),
        active_agenda=_count_active_agenda(),
        failed_workers_recent=_count_recent_failed_workers(),
        paused_sources=_count_paused_sources(),
        crash_storm_latched=_crash_storm_latched(),
    )


# Recent-failure threshold for promoting "ok" → "shaky" in the nudge.
# 3 failures in the last hour is high enough to ignore one-off network
# flakes while still surfacing the autonomy P0 #1 / #2 patterns codex
# flagged on 2026-05-19 (16 of 16 workers failing with the same error).
_RECENT_FAILURE_ESCALATE = 3


def _compose_body(snap: _StatusSnapshot) -> str:
    """Render a 1-2 sentence status from the snapshot.

    Body shape stays under 200 chars so the AU-10 ``MAX_MESSAGE_CHARS``
    (512) cap is never close. Composition must be *honest*: workers
    failing en masse must NOT render as "All good" even when the
    conscience heartbeat says OK — that was the same disease as the
    chat-channel confabulation this job was built to replace.
    """
    if snap.crash_storm_latched:
        return (
            f"Crash storm latched — supervisor refused respawn. "
            f"{snap.active_agenda} agenda · {snap.paused_sources} paused."
        )
    if snap.worst_band == "bad" or snap.failed_workers_recent >= _RECENT_FAILURE_ESCALATE:
        band = snap.worst_band.upper() if snap.worst_band in {"bad", "warn"} else "shaky"
        return (
            f"Status: {band}. {snap.failed_workers_recent} workers failed "
            f"in the last hour · {snap.active_agenda} agenda."
        )
    if snap.worst_band == "warn":
        return (
            f"Status: WARN. {snap.active_agenda} agenda · "
            f"{snap.failed_workers_recent} recent worker failures."
        )
    if snap.worst_band == "ok":
        return (
            f"All good. Conscience OK · {snap.active_agenda} agenda · "
            f"{snap.paused_sources} paused sources."
        )
    # unknown — typically a cold start before the first heartbeat tick
    return (
        f"Heartbeat pending. {snap.active_agenda} agenda · "
        f"{snap.paused_sources} paused sources."
    )


class OperatorNudgeJob(BaseJob):
    """Compose + ship a status toast through the AU-10 OutboundNotifier."""

    uses_llm: ClassVar[bool] = False
    default_model_role: ClassVar[str | None] = None

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        # Off the loop: several directory walks + per-file reads (drift
        # logs, active agenda, active worker records, pause file).
        snap = await asyncio.to_thread(_capture_snapshot)
        body = _compose_body(snap)

        notifier = self._resolve_notifier(ctx)
        if notifier is None:
            duration = (time.monotonic() - t0) * 1000.0
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail="no notifier wired (Mirror app not running?)",
                payload={
                    "skipped": True,
                    "reason": "no_notifier",
                    "snapshot": _snapshot_to_payload(snap),
                },
                duration_ms=duration,
            )

        try:
            result = await notifier.notify("operator_nudge", {"text": body})
        except Exception as exc:  # noqa: BLE001 — never raise from a job
            log.exception("operator_nudge: notify raised")
            duration = (time.monotonic() - t0) * 1000.0
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"notify_raised: {type(exc).__name__}: {exc}"[:200],
                payload={"snapshot": _snapshot_to_payload(snap)},
                duration_ms=duration,
            )

        duration = (time.monotonic() - t0) * 1000.0
        return JobResult(
            job_name=ctx.job_name,
            run_id=ctx.run_id,
            ok=True,
            detail=f"sent={result.sent} skipped={result.skipped} reason={result.reason or '-'}",
            payload={
                "sent": result.sent,
                "skipped": result.skipped,
                "reason": result.reason,
                "errors": result.errors,
                "body": body,
                "snapshot": _snapshot_to_payload(snap),
            },
            duration_ms=duration,
        )

    @staticmethod
    def _resolve_notifier(ctx: JobContext) -> Any | None:
        """Best-effort lookup. The Mirror app caches an OutboundNotifier
        under the ``outbound_notifier`` key; if we're being driven outside
        the Mirror process (REPL test, ad-hoc CLI), there's no app and we
        report a clean skip rather than constructing a half-wired one.
        """
        app = ctx.app
        if app is None:
            return None
        try:
            return app.get("outbound_notifier")
        except Exception:  # noqa: BLE001
            return None


def _snapshot_to_payload(snap: _StatusSnapshot) -> dict[str, Any]:
    return {
        "worst_band": snap.worst_band,
        "active_agenda": snap.active_agenda,
        "failed_workers_recent": snap.failed_workers_recent,
        "paused_sources": snap.paused_sources,
        "crash_storm_latched": snap.crash_storm_latched,
    }


__all__ = ["OperatorNudgeJob"]
