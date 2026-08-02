"""ConscienceHeartbeatJob — daily rule-based drift scrape.

No LLM, no live instrumentation. Scrapes:
  - tesseract/logs/schedule/runs.jsonl   (scheduler failure + idle signals)
  - tesseract/logs/circuit-breakers/*.jsonl  (open-breaker count)

Emits one JSONL line per run to
tesseract/logs/conscience/drift-YYYY-MM-DD.jsonl. The Mirror
`/api/conscience/drift` route reads the latest file for display.

When the worst-status band changes between consecutive reports
(`ok` ↔ `warn` ↔ `bad`), a `conscience_drift` envelope is broadcast
to every live Mirror WS so the operator gets a toast without having
to open the Conscience tab.

Disabled by default in `schedule.yaml` — operator opts in via Mirror
schedule view once they want the tab populated.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from tesseract.paths import TESSERACT_HOME, log_dir
from tesseract.conscience.config import load_drift_config
from tesseract.conscience.drift import evaluate_drift
from tesseract.conscience.memory_writer import (
    count_recent_drifts,
    write_drift_entry,
)
from tesseract.conscience.reader import load_latest_report
from tesseract.orchestrator.autonomy.models import AgendaSource, RiskClass
from tesseract.orchestrator.autonomy.publishers import publish_to_bus
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)



Status = Literal["ok", "warn", "bad"]


class ConscienceHeartbeatJob(BaseJob):
    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            cfg = load_drift_config()
            schedule_log_dir = ctx.log_dir or log_dir("schedule")
            breakers_dir = log_dir("circuit-breakers")
            conscience_dir = _resolve_conscience_dir(ctx)
            enabled_job_count = _count_enabled_jobs(ctx.app)
            report = evaluate_drift(
                schedule_log_dir=schedule_log_dir,
                breakers_dir=breakers_dir,
                thresholds=cfg.thresholds,
                window_hours=cfg.window_hours,
                now=ctx.fired_at,
                enabled_job_count=enabled_job_count,
            )
            report_json = report.to_json()

            # Transition detection must sample previous state BEFORE we
            # append the new line — otherwise we'd diff the new report
            # against itself.
            previous_json = load_latest_report(conscience_dir)
            transition = _detect_transition(previous_json, report_json)

            write_ok = True
            try:
                _write_report(conscience_dir, ctx.fired_at, report_json)
            except OSError:
                # Disk full / permission / locked file. Fail-open on the
                # write, but skip broadcast + mood nudge: firing a
                # transition now would leave the next run comparing
                # against a stale baseline (the last successful write),
                # which could fire a duplicate transition later.
                log.exception(
                    "conscience_heartbeat: drift JSONL write failed; "
                    "skipping broadcast to avoid desync"
                )
                write_ok = False

            delivered = 0
            mood_nudged = False
            drift_memory_id: str | None = None
            drift_flapping = False
            recurrence: dict[int, int] = {}
            if write_ok and transition is not None:
                drift_memory_id, drift_flapping, recurrence = _persist_drift_memory(
                    ctx.app, transition, ctx.fired_at
                )
                if drift_memory_id is not None:
                    transition = {
                        **transition,
                        "memory_id": drift_memory_id,
                        "flapping": drift_flapping,
                        "recurrence_days": recurrence,
                    }
                delivered = await _broadcast_transition(ctx.app, transition)
                mood_nudged = _nudge_mood(ctx.app, transition)
                # AU-20 §10 retrofit — turn the drift transition into a
                # self_reflection agenda candidate. Escalations
                # (ok→warn/bad, warn→bad) route through OPERATOR_GATE
                # since corrective action usually needs operator review;
                # recoveries route through PROPOSE so the kernel can
                # acknowledge without gating.
                _publish_drift_transition(transition, ctx)

            summary = report.summary
            detail = (
                f"signals={len(report.signals)} "
                f"ok={summary['ok']} warn={summary['warn']} bad={summary['bad']}"
                + ("" if write_ok else " write_failed=true")
                + (f" transition={transition['from']}->{transition['to']} delivered={delivered}"
                   if write_ok and transition else "")
                + (f" mem={drift_memory_id}" if drift_memory_id else "")
                + (" flap=true" if drift_flapping else "")
            )
            payload: dict[str, Any] = {
                "summary": summary,
                "window_hours": cfg.window_hours,
                "enabled_job_count": enabled_job_count,
                "write_ok": write_ok,
            }
            if write_ok and transition is not None:
                payload["transition"] = transition
                payload["delivered_ws_count"] = delivered
                payload["mood_nudged"] = mood_nudged
                if drift_memory_id is not None:
                    payload["drift_memory_id"] = drift_memory_id
                    payload["drift_flapping"] = drift_flapping
                    payload["drift_recurrence_days"] = recurrence
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=write_ok,
                detail=detail,
                payload=payload,
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("conscience_heartbeat crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )


_ESCALATIONS: frozenset[tuple[Status, Status]] = frozenset({
    ("ok", "warn"),
    ("ok", "bad"),
    ("warn", "bad"),
})


def _publish_drift_transition(transition: dict, ctx: JobContext) -> None:
    """One-line bus publish per AU-20 §10. Escalation routes through
    OPERATOR_GATE (operator usually needs to act); recovery routes
    through PROPOSE (kernel acknowledges, may auto-resolve). No-op when
    no bus is registered."""
    frm = transition.get("from")
    to = transition.get("to")
    is_escalation = (frm, to) in _ESCALATIONS
    risk = RiskClass.OPERATOR_GATE if is_escalation else RiskClass.PROPOSE
    changed = transition.get("changed_signals") or []
    evidence_ids = [str(c.get("name") or "") for c in changed if c.get("name")][:8]
    memory_id = transition.get("memory_id")
    if memory_id:
        evidence_ids.append(str(memory_id))
    payload = {
        "observation": (
            f"conscience drift {frm}->{to}; "
            f"signals_changed={len(changed)} flapping={bool(transition.get('flapping'))}"
        ),
        "suggested_risk_class": risk.value,
        "evidence_ids": evidence_ids,
        "memory_id": memory_id,
        "source_handler": "conscience_heartbeat",
    }
    # event_id keys on the transition pair + run_id so back-to-back
    # ticks emitting the same pair don't collide; new transitions get
    # a fresh agenda candidate.
    publish_to_bus(
        AgendaSource.SELF_REFLECTION,
        payload,
        event_id=f"evt_conscience_{frm}_{to}_{ctx.run_id[:12]}",
    )


def _resolve_conscience_dir(ctx: JobContext) -> Path:
    # Tests drop the engine and inject a tmp schedule log dir via ctx.log_dir.
    # When they do, keep conscience output in the same tmp tree so fixtures
    # don't leak into the repo's tesseract/logs/conscience/.
    if ctx.log_dir is not None:
        return Path(ctx.log_dir).parent / "conscience"
    return log_dir("conscience")


def _count_enabled_jobs(app: Any) -> int | None:
    """Return the count of enabled jobs in the live scheduler registry.

    `None` when the scheduler isn't mounted (REPL / tests without an
    `app["scheduler"]`) — `evaluate_drift` then falls back to its plain
    classifier. `0` is meaningful: it fires the `no_enabled_jobs`
    carve-out in `drift._signal_idle_hours`.
    """
    if app is None or not hasattr(app, "get"):
        return None
    scheduler = app.get("scheduler")
    if scheduler is None:
        return None
    registry = getattr(scheduler, "registry", None)
    if registry is None:
        return None
    try:
        return sum(1 for rt in registry.values() if getattr(rt, "enabled", False))
    except Exception:  # noqa: BLE001 — defensive; never fail the heartbeat
        return None


def _write_report(target_dir: Path, when: datetime, record: dict) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = when.astimezone(timezone.utc).date().isoformat()
    target = target_dir / f"drift-{stamp}.jsonl"
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _worst_status(summary: dict[str, int]) -> Status:
    if summary.get("bad", 0) > 0:
        return "bad"
    if summary.get("warn", 0) > 0:
        return "warn"
    return "ok"


def _detect_transition(previous: dict | None, current: dict) -> dict | None:
    """Return a transition record when the worst-status band changed.

    Shape: {from, to, summary, changed_signals}. Returns None when
    there's no previous report (first heartbeat is never a transition —
    would spam on first-ever run) or when the band stayed put.
    """
    if previous is None:
        return None
    prev_summary = previous.get("summary") or {}
    curr_summary = current.get("summary") or {}
    prev_status = _worst_status(prev_summary)
    curr_status = _worst_status(curr_summary)
    if prev_status == curr_status:
        return None

    prev_signals = {s["name"]: s for s in (previous.get("signals") or [])}
    curr_signals = {s["name"]: s for s in (current.get("signals") or [])}
    changed: list[dict[str, Any]] = []
    for name, sig in curr_signals.items():
        prev_sig = prev_signals.get(name)
        if prev_sig is None or prev_sig.get("status") != sig.get("status"):
            changed.append({
                "name": name,
                "from": (prev_sig or {}).get("status", "unknown"),
                "to": sig.get("status"),
                "value": sig.get("value"),
                "detail": sig.get("detail", ""),
            })
    return {
        "from": prev_status,
        "to": curr_status,
        "summary": curr_summary,
        "changed_signals": changed,
    }


async def _broadcast_transition(app: Any, transition: dict) -> int:
    """Surface a transition through three channels:

    1. Per-session `ChatSession.ingest_conscience_transition` — queues a
       synthetic `[conscience_drift]` note for next-turn injection so
       TARS himself becomes aware. Happens for every session whether or
       not a WS is attached.
    2. `conscience_drift` envelope broadcast to every live WS — the
       Mirror frontend dispatches to a toast so the operator is alerted
       without opening the Conscience tab.

    Returns the WS delivery count (channel 2). Channel 1 silently
    touches every session with a `chat_session` attribute.
    """
    sessions = _server_sessions(app)
    if not sessions:
        return 0

    # Channel 1 — inject into each session's chat_session.
    for sess in sessions.values():
        chat_session = getattr(sess, "chat_session", None)
        if chat_session is None or not hasattr(chat_session, "ingest_conscience_transition"):
            continue
        try:
            chat_session.ingest_conscience_transition(transition)
        except Exception:
            log.exception(
                "conscience_heartbeat: ingest_conscience_transition failed for %s",
                getattr(sess, "session_id", "?"),
            )

    # Channel 2 — WS envelope fan-out.
    from tesseract.mirror.server.envelope import make_envelope
    from tesseract.mirror.server.session import send_envelope

    delivered = 0
    for sess in sessions.values():
        env = make_envelope(
            "conscience_drift",
            "background",
            getattr(sess, "session_id", ""),
            transition,
        )
        try:
            await send_envelope(sess, env)
            delivered += 1
        except Exception:
            log.exception(
                "conscience_heartbeat: send_envelope failed for %s",
                getattr(sess, "session_id", "?"),
            )
    return delivered


def _server_sessions(app: Any) -> dict[str, Any]:
    if app is None or not hasattr(app, "get"):
        return {}
    return app.get("server_sessions") or {}


def _resolve_memory_store(app: Any) -> Any:
    """Pull `MemoryStore` off the live app via `memory_bundle`.

    Returns `None` when the bundle is absent (CLI-only scheduler runs,
    bare-bones tests). Drift memory writes are best-effort: the
    JSONL audit trail and WS toast still fire even if memory persistence
    can't.
    """
    if app is None or not hasattr(app, "get"):
        return None
    bundle = app.get("memory_bundle")
    if bundle is None:
        return None
    return getattr(bundle, "store", None)


def _persist_drift_memory(
    app: Any,
    transition: dict,
    when: datetime,
) -> tuple[str | None, bool, dict[int, int]]:
    """Land the drift in `memory-store/conscience/drift/` and compute recurrence.

    Returns `(memory_id, flapping_flag, recurrence_counts)`. Each element
    is empty/None when the memory bundle isn't attached or the writer
    declined the entry.
    """
    store = _resolve_memory_store(app)
    if store is None:
        return None, False, {}
    try:
        result = write_drift_entry(store=store, transition=transition, when=when)
    except Exception:
        log.exception("conscience_heartbeat: drift memory write failed")
        return None, False, {}
    if result is None:
        return None, False, {}
    try:
        recurrence = count_recent_drifts(
            store=store,
            signal_name=result.primary_signal,
            now=when,
        )
    except Exception:
        log.exception("conscience_heartbeat: recurrence count failed")
        recurrence = {}
    return result.memory_id, result.flapping, recurrence


_MOOD_NUDGES: dict[tuple[Status, Status], tuple[float, float]] = {
    # (from, to): (valence_delta, intensity_delta).
    # Escalation — TARS gets darker + a touch more agitated.
    ("ok", "warn"): (-0.15, +0.05),
    ("ok", "bad"): (-0.30, +0.10),
    ("warn", "bad"): (-0.20, +0.10),
    # Recovery — valence climbs back, intensity settles toward baseline.
    ("warn", "ok"): (+0.15, -0.05),
    ("bad", "warn"): (+0.15, -0.05),
    ("bad", "ok"): (+0.30, -0.10),
}


def _nudge_mood(app: Any, transition: dict) -> bool:
    """Shift `app["mood"]` by a small delta on transition.

    Returns `True` when a delta was applied, `False` when no mood
    object is attached or the transition pair isn't in `_MOOD_NUDGES`.
    `MoodState.set` clamps, so repeated escalations saturate rather
    than overflow.
    """
    if app is None or not hasattr(app, "get"):
        return False
    mood = app.get("mood")
    if mood is None or not hasattr(mood, "set"):
        return False
    frm = transition.get("from")
    to = transition.get("to")
    delta = _MOOD_NUDGES.get((frm, to))  # type: ignore[arg-type]
    if delta is None:
        if frm is not None and to is not None:
            # Structurally unreachable today — _detect_transition only
            # emits the 6 pairs covered above. Fires if a future Status
            # literal is added without updating the dict.
            log.warning(
                "conscience_heartbeat: no mood nudge for transition %s->%s", frm, to
            )
        return False
    v_delta, i_delta = delta
    try:
        mood.set(
            getattr(mood, "intensity", 0.5) + i_delta,
            getattr(mood, "valence", 0.0) + v_delta,
        )
        return True
    except Exception:  # noqa: BLE001 — defensive
        log.exception("conscience_heartbeat: mood nudge failed")
        return False
