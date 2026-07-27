"""StrategistReaperJob — AU-23 Session 3 — expire stale initiatives.

Transitions strategist-sourced agenda items whose `horizon_days` has
elapsed AND are still in PROPOSED / AWAITING_OPERATOR to ABANDONED with
`reason="initiative_expired"`. Sub-second daily sweep.

The reaper does NOT call any model. It only walks the agenda active
dir, joins each strategist item against the strategist-seen.jsonl
ledger by normalised-goal hash to recover `horizon_days`, and
transitions the expired ones. Items in RUNNING / DONE / BLOCKED etc.
are intentionally untouched — once the operator approves and dispatch
fires, the initiative escapes the reaper.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.models import (
    AgendaSource,
    AgendaStatus,
)
from tesseract.orchestrator.autonomy.strategist import (
    DEFAULT_HORIZON_DAYS,
    goal_key,
    seen_ledger_path,
)
from tesseract.paths import TESSERACT_HOME
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


_REAPABLE = frozenset({AgendaStatus.PROPOSED, AgendaStatus.AWAITING_OPERATOR})


class StrategistReaperJob(BaseJob):
    uses_llm = False

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            now = ctx.fired_at
            home_override = _resolve_home_override(ctx)
            ledger_path = (
                Path(ctx.config["seen_path"])
                if ctx.config.get("seen_path")
                else seen_ledger_path(home_override)
            )
            horizon_map = _load_horizon_map(ledger_path)

            store = _resolve_agenda_store(ctx)
            if store is None:
                return _ok(
                    ctx, t0,
                    detail="agenda_store_unavailable",
                    payload={"scanned": 0, "expired": 0},
                )

            scanned = 0
            expired = 0
            expired_ids: list[str] = []
            for item in store.iter_active():
                if item.source is not AgendaSource.STRATEGIST:
                    continue
                if item.status not in _REAPABLE:
                    continue
                scanned += 1
                horizon = horizon_map.get(goal_key(item.goal), DEFAULT_HORIZON_DAYS)
                created = item.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                deadline = created + timedelta(days=max(1, horizon))
                if now < deadline:
                    continue
                store.transition(
                    item,
                    AgendaStatus.ABANDONED,
                    reason="initiative_expired",
                    by="recovery",
                )
                expired += 1
                expired_ids.append(item.id)

            return _ok(
                ctx, t0,
                detail=f"scanned={scanned} expired={expired}",
                payload={
                    "scanned": scanned,
                    "expired": expired,
                    "expired_ids": expired_ids,
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("strategist_reaper crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )


# ── helpers ─────────────────────────────────────────────────────────


def _ok(ctx: JobContext, t0: float, *, detail: str, payload: dict[str, Any]) -> JobResult:
    return JobResult(
        job_name=ctx.job_name,
        run_id=ctx.run_id,
        ok=True,
        detail=detail,
        payload=payload,
        duration_ms=(time.monotonic() - t0) * 1000.0,
    )


def _resolve_home_override(ctx: JobContext) -> Path | None:
    override = ctx.config.get("tesseract_home")
    return Path(override) if override else None


def _resolve_agenda_store(ctx: JobContext) -> AgendaStore | None:
    """Prefer the live store mounted on the app (single in-process
    instance with the right weights); fall back to constructing one so
    CLI-only runs still work. Path resolution happens inside AgendaStore
    via the agenda/paths.py helpers — those read TESSERACT_HOME at call
    time, so monkeypatched test envs route correctly."""
    app = ctx.app
    if app is not None and hasattr(app, "get"):
        live = app.get("agenda_store")
        if live is not None:
            return live
    try:
        return AgendaStore()
    except Exception:  # noqa: BLE001
        log.exception("strategist_reaper: AgendaStore() init failed")
        return None


def _load_horizon_map(path: Path) -> dict[str, int]:
    """Walk the ledger keeping the most recent horizon per key."""
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
                key = str(row.get("key") or "")
                if not key:
                    continue
                try:
                    horizon = int(row.get("horizon_days") or DEFAULT_HORIZON_DAYS)
                except (TypeError, ValueError):
                    horizon = DEFAULT_HORIZON_DAYS
                out[key] = horizon
    except OSError:
        log.exception("strategist_reaper: ledger read failed")
        return {}
    return out


__all__ = ["StrategistReaperJob"]
