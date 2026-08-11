"""RecoveryManager — orchestrates the boot-time scans.

S1 shipped scans 1-4 (missions [removed — prune wave 1], workers stub,
PTY leases [removed — prune wave 1], scheduler runs). S2 adds:

- scan 5 (agenda) — stub until AU-4 ships the AgendaStore. Surfaces
  zero counts so the dashboard renders consistently.

The tool-proposal and upgrade-continuation scans (prior scans 6-7)
were removed with the forge/upgrades self-modification stack (prune
wave 1, Batch 2) — new tools are built via delegation and promoted by
hand, so there is no provisional-tier / continuation state left to
scan for on boot.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tesseract.bootid import current_boot_id, mint_boot_id
from tesseract.orchestrator.workers.record import WorkerStatus
from tesseract.orchestrator.recovery.summary import (
    RecoverySummary,
    build_recovery_event,
    empty_scan_counts,
)
from tesseract.paths import TESSERACT_HOME, home_logs_root, log_dir
from tesseract.workspace_events import EventStore, WorkspaceEvent

log = logging.getLogger(__name__)

# How far back to look at runs.jsonl for the schedule scan. AU-2 S2 will
# replace this with downtime detection driven by the last clean-shutdown
# intent timestamp; for S1, 24h is a reasonable proxy for "recent".
_SCHEDULE_LOOKBACK = timedelta(hours=24)


def boot_id(*, now: datetime | None = None) -> str:
    """This process's boot id — the same one the backend log is named after.

    Format: ``boot-YYYYMMDDTHHMMSS-<8 hex>``. Called bare it returns the
    process-wide id (``bootid.current_boot_id``), so a recovery summary and
    the log file covering that boot carry one identifier and can be joined.
    Passing ``now`` mints a fresh id instead, which is what fixtures driving
    recovery repeatedly want.
    """
    if now is not None:
        return mint_boot_id(now=now)
    return current_boot_id()


def new_recovery_manager(
    *,
    tesseract_home: Path | None = None,
) -> "RecoveryManager":
    """Build a RecoveryManager with default wiring. Tests pass
    overrides so file paths land in tmp_path.

    Every scan only reads from disk — no live-substrate dependency.
    """
    home = (tesseract_home or TESSERACT_HOME).resolve()
    schedule_log_dir = log_dir("schedule")
    workspace_logs_dir = home_logs_root()
    return RecoveryManager(
        tesseract_home=home,
        schedule_log_dir=schedule_log_dir,
        workspace_logs_dir=workspace_logs_dir,
    )


class RecoveryManager:
    """Orchestrate the boot-time scans and emit one workspace event.

    Construct via :func:`new_recovery_manager` in production. Tests
    inject paths directly. Idempotent: running ``run()`` twice on the
    same state produces identical transitions (proven by the test
    suite).
    """

    def __init__(
        self,
        *,
        tesseract_home: Path,
        schedule_log_dir: Path,
        workspace_logs_dir: Path,
    ) -> None:
        self.tesseract_home = tesseract_home
        self.schedule_log_dir = schedule_log_dir
        self.workspace_logs_dir = workspace_logs_dir
        self._event_store: EventStore | None = None

    # -- public surface --------------------------------------------------

    async def run(
        self,
        *,
        downtime_seconds: float = 0.0,
        emit_event: bool = True,
    ) -> RecoverySummary:
        """Execute every scan in order. Returns the populated summary
        and (when ``emit_event=True``) appends a ``recovery_summary``
        workspace event so the dashboard + operator inbox pick it up.

        Failure-isolation: each scan runs under its own try/except so
        one broken scan doesn't blow up the others. Each scan is
        additive — it extends the summary without mutating prior
        buckets.
        """
        t_start = time.monotonic()
        summary = RecoverySummary(
            boot_id=boot_id(),
            started_at=datetime.now(timezone.utc),
            downtime_seconds=downtime_seconds,
            scans=empty_scan_counts(),
        )

        for name, scan in (
            ("workers", self._scan_workers),
            ("schedule", self._scan_schedule),
            ("agenda", self._scan_agenda),
        ):
            try:
                scan(summary)
            except Exception as exc:  # noqa: BLE001 — every scan stays soft
                log.exception("recovery: scan %s failed", name)
                summary.flag(kind="scan_error", id=name, reason=str(exc))

        log.info(
            "recovery: complete in %.2fs (boot=%s) scans=%s attn=%d",
            time.monotonic() - t_start, summary.boot_id,
            summary.scans, len(summary.operator_attention),
        )

        if emit_event:
            try:
                event = build_recovery_event(summary)
                store = self._get_event_store()
                store.append_event(event)
            except Exception:  # noqa: BLE001 — event-emit failure must not block boot
                log.exception("recovery: append_event failed")

        return summary

    # -- scan: workers ---------------------------------------------------

    def _scan_workers(self, summary: RecoverySummary) -> None:
        """AU-3 S2 — walk every durable worker record under
        ``<TESSERACT_HOME>/workers/active/`` and classify.

        Recovery IS read-only at the directory-walk layer: the count-only
        ``iter_active_status_summary`` peek doesn't mutate anything. The
        per-record state machine, however, persists transitions through
        ``recover_worker`` — that's the canonical write path for the
        recovery decision (record + archive). The invariant that broke
        S1 (silent legacy unlinks) doesn't apply: every write here is
        an explicit terminal-state transition with a documented reason.
        """
        from tesseract.orchestrator.workers.record import (
            iter_active_status_summary,
        )

        summary.section("workers")
        for worker_id, _kind, status in iter_active_status_summary():
            if status in {
                WorkerStatus.DONE.value,
                WorkerStatus.FAILED.value,
                WorkerStatus.BLOCKED.value,
                WorkerStatus.INTERRUPTED.value,
                WorkerStatus.CANCELLED.value,
            }:
                summary.inc("workers", "preserved")
                continue
            try:
                self._recover_one_worker(worker_id, summary)
            except Exception:  # noqa: BLE001 — single-worker failure must not blow the scan
                log.exception("recovery: worker scan failed for %s", worker_id)
                summary.flag(
                    kind="worker",
                    id=worker_id,
                    reason="scan_error",
                )

    def _recover_one_worker(
        self,
        worker_id: str,
        summary: RecoverySummary,
    ) -> None:
        """Drive the per-kind recovery handler synchronously inside the
        scan loop. ``recover_worker_sync`` handles the async-bridge
        details (nested-loop test harnesses thread-pool around the
        async handler so AU-5 can keep awaiting real IO in resume)."""
        from tesseract.orchestrator.workers.recovery import recover_worker_sync

        result = recover_worker_sync(worker_id)
        if result is None:
            summary.flag(
                kind="worker",
                id=worker_id,
                reason="record_missing_at_recovery",
            )
            return
        if result.status == WorkerStatus.INTERRUPTED:
            summary.inc("workers", "interrupted")
            summary.flag(
                kind="worker",
                id=worker_id,
                reason=result.status_history[-1].reason if result.status_history else "interrupted",
            )
        elif result.status == WorkerStatus.RUNNING:
            summary.inc("workers", "preserved")
        else:
            summary.inc("workers", "preserved")

    # -- scan: scheduler runs ---------------------------------------------

    def _scan_schedule(self, summary: RecoverySummary) -> None:
        """Read runs.jsonl from the past 24h and surface aggregate
        completion counts.

        Buckets are ``completed`` (ok=True) and ``failed`` (ok=False) —
        these are the rows that actually landed on disk, which the
        engine writes AFTER each run finishes. Crash-interrupted
        firings leave no row at all in runs.jsonl (engine doesn't
        write a "started" marker), so ``interrupted`` cannot be
        derived from this log alone in S1. AU-2 S2 will add the
        started-marker instrumentation and introduce a third bucket.
        """
        log_path = self.schedule_log_dir / "runs.jsonl"
        if not log_path.exists():
            return
        cutoff = datetime.now(timezone.utc) - _SCHEDULE_LOOKBACK
        completed = 0
        failed = 0
        try:
            with log_path.open("r", encoding="utf-8") as fh:
                for raw_line in fh:
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        row = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    fired_raw = row.get("fired_at") or ""
                    try:
                        fired = datetime.fromisoformat(fired_raw)
                    except ValueError:
                        continue
                    if fired.tzinfo is None:
                        fired = fired.replace(tzinfo=timezone.utc)
                    if fired < cutoff:
                        continue
                    if row.get("ok") is False:
                        failed += 1
                    else:
                        completed += 1
        except OSError:
            log.exception("recovery: schedule scan failed")
            return
        summary.inc("schedule", "completed", by=completed)
        summary.inc("schedule", "failed", by=failed)

    # -- scan: agenda -----------------------------------------------------

    def _scan_agenda(self, summary: RecoverySummary) -> None:
        """AU-4 — walk ``<TESSERACT_HOME>/agenda/active/*.json`` and
        apply the recovery transition map per ``_shared/recovery-state-
        machine.md §5``:

        - ``selected`` / ``running`` with all linked workers terminal →
          ``resume_queued`` (kernel re-derives outcome from worker state).
        - ``selected`` / ``running`` with at least one linked worker
          interrupted → ``resume_queued`` (retry budget lives at the
          kernel; recovery never reads the budget).
        - ``awaiting_operator`` preserved; surfaced in attention.
        - terminal items preserved (no-op, but counted).

        Per the protocol, recovery never WRITES new attention to the
        item file; it just transitions the status field. The kernel
        (AU-5) inspects status + linked workers to decide next move.
        Worker linkage is enumerated against the AU-3 worker records on
        disk via ``load_record`` — fresh data, not the agenda's cached
        linked_workers list (which could be stale).
        """
        summary.section("agenda")
        from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
        from tesseract.orchestrator.autonomy.models import AgendaStatus
        from tesseract.orchestrator.workers.record import (
            WorkerStatus,
            load_record,
        )

        store = AgendaStore()
        live = {
            AgendaStatus.SELECTED,
            AgendaStatus.RUNNING,
        }
        worker_terminal = {
            WorkerStatus.DONE.value,
            WorkerStatus.FAILED.value,
            WorkerStatus.BLOCKED.value,
            WorkerStatus.INTERRUPTED.value,
            WorkerStatus.CANCELLED.value,
        }
        for item in store.iter_active():
            if item.status == AgendaStatus.AWAITING_OPERATOR:
                summary.inc("agenda", "preserved")
                summary.flag(
                    kind="agenda",
                    id=item.id,
                    reason="awaiting_operator_at_restart",
                )
                continue
            if item.status == AgendaStatus.RESUME_QUEUED:
                summary.inc("agenda", "resume_queued")
                continue
            if item.status not in live:
                summary.inc("agenda", "preserved")
                continue

            interrupted = False
            all_terminal = True
            for worker_id in item.linked_workers:
                try:
                    record = load_record(worker_id)
                except Exception:  # noqa: BLE001 — single record failure must not blow the scan
                    record = None
                if record is None:
                    interrupted = True
                    all_terminal = False
                    continue
                if record.status == WorkerStatus.INTERRUPTED:
                    interrupted = True
                if record.status.value not in worker_terminal:
                    all_terminal = False

            if interrupted or (item.linked_workers and all_terminal):
                store.transition(
                    item,
                    AgendaStatus.RESUME_QUEUED,
                    reason="agenda_resume",
                    by="recovery",
                )
                summary.inc("agenda", "resume_queued")
                summary.flag(
                    kind="agenda",
                    id=item.id,
                    reason="agenda_resume",
                )
            else:
                summary.inc("agenda", "preserved")

    # -- helpers --------------------------------------------------------

    def _get_event_store(self) -> EventStore:
        if self._event_store is None:
            self._event_store = EventStore(self.workspace_logs_dir)
        return self._event_store


__all__ = [
    "RecoveryManager",
    "boot_id",
    "new_recovery_manager",
]
