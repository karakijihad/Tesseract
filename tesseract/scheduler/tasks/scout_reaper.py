"""ScoutReaperJob — P7 Task 2b — expire stale SCOUT proposals.

Transitions SCOUT-sourced agenda items still sitting UNVETTED / PROPOSED /
AWAITING_OPERATOR past their staleness horizon to ABANDONED with
``reason="scout_proposal_expired"``. Sub-second sweep; calls no model.

A **separate job from ``strategist_reaper.py``**, by design (see
``tesseract/scheduler/tasks/scout.py``'s module docstring for the
producer side). ``strategist_reaper`` is wired tightly to STRATEGIST-only
semantics: it imports ``DEFAULT_HORIZON_DAYS`` / ``goal_key`` /
``seen_ledger_path`` from ``strategist.py`` and joins on a hash of the
item's own goal text, because each strategist initiative carries its own
per-item ``horizon_days``. Scout's horizon is a single global
``staleness_days`` read once per run from the scout job's own config (not
per-item), and the join key it needs (``source_event_id``, already unique
and exact) is simpler than a goal-text hash. Generalising
``strategist_reaper`` into a multi-source reaper would mean reshaping a
file whose STRATEGIST-only contract is already locked by prior P7 work,
for a saving that doesn't materialise here — the two reapers share a
*pattern* (side-ledger + horizon join + transition-to-ABANDONED), not
enough concrete code to be worth merging. Keeping them separate also
keeps each reaper independently testable and toggle-able in
``schedule.yaml``, matching the existing one-handler-per-concern shape of
the scheduler tree.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.models import AgendaSource, AgendaStatus
from tesseract.paths import TESSERACT_HOME
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

_REAPABLE = frozenset({AgendaStatus.UNVETTED, AgendaStatus.PROPOSED, AgendaStatus.AWAITING_OPERATOR})


class ScoutReaperJob(BaseJob):
    uses_llm = False

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            now = ctx.fired_at
            ledger_path = _resolve_horizon_path(ctx)
            horizon_map = _load_horizon_map(ledger_path)

            store = _resolve_agenda_store(ctx)
            if store is None:
                return _ok(
                    ctx, t0, detail="agenda_store_unavailable",
                    payload={"scanned": 0, "expired": 0},
                )

            scanned = 0
            expired = 0
            expired_ids: list[str] = []
            for item in store.iter_active():
                if item.source is not AgendaSource.SCOUT:
                    continue
                if item.status not in _REAPABLE:
                    continue
                scanned += 1
                horizon = horizon_map.get(item.source_event_id or "")
                if horizon is None:
                    continue  # never published via the horizon ledger — leave it alone
                created = item.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                deadline = created + timedelta(days=max(1, horizon))
                if now < deadline:
                    continue
                store.transition(
                    item, AgendaStatus.ABANDONED, reason="scout_proposal_expired", by="recovery",
                )
                expired += 1
                expired_ids.append(item.id)

            return _ok(
                ctx, t0, detail=f"scanned={scanned} expired={expired}",
                payload={"scanned": scanned, "expired": expired, "expired_ids": expired_ids},
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("scout_reaper crashed")
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


def _resolve_horizon_path(ctx: JobContext) -> Path:
    override = ctx.config.get("horizon_path")
    if override:
        return Path(override)
    home_override = os.environ.get("TESSERACT_HOME")
    home = Path(home_override).resolve() if home_override else TESSERACT_HOME
    return home / "autonomy" / "scout-horizon.jsonl"


def _resolve_agenda_store(ctx: JobContext) -> AgendaStore | None:
    app = ctx.app
    if app is not None and hasattr(app, "get"):
        live = app.get("agenda_store")
        if live is not None:
            return live
    try:
        return AgendaStore()
    except Exception:  # noqa: BLE001
        log.exception("scout_reaper: AgendaStore() init failed")
        return None


def _load_horizon_map(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    out: dict[str, int] = {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_id = str(row.get("event_id") or "")
                if not event_id:
                    continue
                try:
                    horizon = int(row.get("staleness_days"))
                except (TypeError, ValueError):
                    continue
                out[event_id] = horizon
    except OSError:
        log.exception("scout_reaper: ledger read failed")
        return {}
    return out


__all__ = ["ScoutReaperJob"]
