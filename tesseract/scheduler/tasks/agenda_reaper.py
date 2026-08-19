"""AgendaReaperJob — staleness self-prune for the agenda backlog.

Runs as the `agenda_reap` stage of the `consolidate` row. It had a 12:30 row
of its own and no reason for one: nothing it sweeps is time-of-day sensitive,
and an hour chosen on one machine shipped to every install.

Transitions stale non-terminal agenda items to ABANDONED, per a
per-status ``max_age_days`` config map, so a growing non-terminal
backlog can never again starve fresh admission against
``max_open_total`` (live-gate finding: ~87 stale items held the agenda
against a cap of 40). Age is measured from each item's *last* status
transition (``status_history[-1].at``), not ``created_at``, so an item
someone recently touched survives even if it was minted long ago.
Sub-second sweep; calls no model, sends no channel message, writes no
memory — read store, transition, ``JobResult``.

**It is the only reaper now, and that is why the table below is empty.** Two
dedicated reapers used to own particular statuses for particular sources, and
this job skipped those pairs to avoid applying two staleness policies to one
item. Both were deleted with the sources they served. An exemption is a promise
that something else will clean the item, so an exemption with no dedicated
reaper behind it is how an item lives forever — the table stays as the
mechanism, with nothing in it, because the next dedicated reaper will need it.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta, timezone
from typing import Any

from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.models import AgendaSource, AgendaStatus
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

# Statuses owned by a source's dedicated reaper — never double-handled here.
# Any status for that source NOT in this set falls through to the general sweep
# below. Empty today: every source that had a dedicated reaper is gone.
_BUILTIN_OWNED_STATUSES: dict[AgendaSource, frozenset[AgendaStatus]] = {}


class AgendaReaperJob(BaseJob):
    uses_llm = False

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            if "max_age_days" not in ctx.config:
                return _fail(ctx, t0, "missing config: max_age_days")
            max_age_map = _parse_max_age_days(ctx.config["max_age_days"])
            exempt_sources = _parse_exempt_sources(ctx.config.get("exempt_sources") or [])

            store = _resolve_agenda_store(ctx)
            if store is None:
                return _ok(
                    ctx, t0, detail="agenda_store_unavailable",
                    payload={"reaped": 0, "skipped": 0},
                )

            now = ctx.fired_at
            reaped_by_status: dict[str, int] = {}
            skipped = 0
            for item in store.iter_active():
                threshold = max_age_map.get(item.status)
                if threshold is None:
                    continue
                last_at = item.status_history[-1].at if item.status_history else item.created_at
                if last_at.tzinfo is None:
                    last_at = last_at.replace(tzinfo=timezone.utc)
                if now - last_at < timedelta(days=threshold):
                    continue
                if item.source in exempt_sources:
                    skipped += 1
                    continue
                owned_statuses = _BUILTIN_OWNED_STATUSES.get(item.source)
                if owned_statuses is not None and item.status in owned_statuses:
                    skipped += 1
                    continue
                stale_status = item.status.value
                store.transition(
                    item,
                    AgendaStatus.ABANDONED,
                    reason=f"agenda_reaper: stale {stale_status} > {threshold}d",
                    by="kernel",
                )
                reaped_by_status[stale_status] = reaped_by_status.get(stale_status, 0) + 1

            total = sum(reaped_by_status.values())
            if total == 0 and skipped == 0:
                detail = "idle"
            else:
                parts = " ".join(f"{status}={count}" for status, count in sorted(reaped_by_status.items()))
                counts = f" ({parts})" if parts else ""
                detail = f"reaped {total}{counts} skipped={skipped} exempt"
            return _ok(
                ctx, t0, detail=detail,
                payload={"reaped": total, "skipped": skipped, "reaped_by_status": reaped_by_status},
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("agenda_reaper crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )


def _ok(ctx: JobContext, t0: float, *, detail: str, payload: dict[str, Any]) -> JobResult:
    return JobResult(
        job_name=ctx.job_name,
        run_id=ctx.run_id,
        ok=True,
        detail=detail,
        payload=payload,
        duration_ms=(time.monotonic() - t0) * 1000.0,
    )


def _fail(ctx: JobContext, t0: float, detail: str) -> JobResult:
    return JobResult(
        job_name=ctx.job_name,
        run_id=ctx.run_id,
        ok=False,
        detail=detail,
        duration_ms=(time.monotonic() - t0) * 1000.0,
    )


def _parse_max_age_days(raw: Any) -> dict[AgendaStatus, int]:
    return {AgendaStatus(str(status)): int(days) for status, days in dict(raw).items()}


def _parse_exempt_sources(raw: list[Any]) -> frozenset[AgendaSource]:
    return frozenset(AgendaSource(str(source)) for source in raw)


def _resolve_agenda_store(ctx: JobContext) -> AgendaStore | None:
    app = ctx.app
    if app is not None and hasattr(app, "get"):
        live = app.get("agenda_store")
        if live is not None:
            return live
    try:
        return AgendaStore()
    except Exception:  # noqa: BLE001
        log.exception("agenda_reaper: AgendaStore() init failed")
        return None


__all__ = ["AgendaReaperJob"]
