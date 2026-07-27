"""AutonomyHeartbeatJob — AU-20 — LLM agenda-candidate emitter.

General-purpose heartbeat. Each tick:

1. Reads workspace events + memory writes since last cursor.
2. Idle short-circuit when both deltas are empty (no model bill).
3. Asks the role-chained adapter for 0-3 bounded observations.
4. For each accepted observation:
   - Writes a ``MemoryType.CONSCIENCE`` entry under
     ``conscience/autonomy/`` so the operator can see it in
     ``memory_search`` and the Obsidian graph.
   - Publishes one :class:`AutonomyEvent` under
     ``AgendaSource.SELF_REFLECTION`` — the AU-5 kernel mapper folds
     it into an :class:`AgendaItem`.
5. Advances the cursor regardless of observation count so the same
   activity is not re-fed next tick.

Dedup: SHA-1 of normalized observation text + 24h rolling window in
``<TESSERACT_HOME>/autonomy/heartbeat-seen.jsonl``. Cursor in
``<TESSERACT_HOME>/autonomy/heartbeat-cursor.json``.

Disabled by default in ``schedule.yaml``. Operator flips it on once
``api.nim.gpt_oss_120b`` (or whatever role primary points at) is reachable.

Distinct from existing heartbeats:
- ``conscience_heartbeat`` (rule-based drift scrape, no LLM)
- ``librarian_heartbeat`` (memory consolidation + personality distillation)
- ``dream_cycle`` (nightly recall log → MEMORY.md promotion)
- ``feedback_consolidator`` (weekly feedback merge/soul/archive proposals)
- ``observer_idle`` (per-idle-session observer.observe fire)

None of those emit agenda candidates — that gap is what AU-20 fills.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field, ValidationError, field_validator

from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.memory.types import MemoryFrontmatter, MemoryType, Stability
from tesseract.orchestrator.autonomy.models import AgendaSource, RiskClass
from tesseract.orchestrator.autonomy.publishers import publish_to_bus
from tesseract.paths import TESSERACT_HOME
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.role_chain import build_chain_for_job
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_OBSERVATIONS = 3
DEFAULT_DEDUPE_WINDOW_HOURS = 24
DEFAULT_MAX_EVIDENCE_PER_OBS = 8
DEFAULT_INPUT_EVENT_CAP = 20
DEFAULT_INPUT_MEMORY_CAP = 20
DEFAULT_BODY_MAX_CHARS = 4000

CONSCIENCE_SUBDIR = "conscience/autonomy"
HEARTBEAT_TAG = "autonomy_heartbeat"

_ALLOWED_RISK = frozenset({rc.value for rc in RiskClass})


class _Observation(BaseModel):
    """Single observation from the heartbeat adapter call."""

    observation: str = Field(min_length=10, max_length=600)
    suggested_risk_class: str = Field(default=RiskClass.PROPOSE.value)
    evidence_ids: list[str] = Field(default_factory=list)

    @field_validator("suggested_risk_class")
    @classmethod
    def _coerce_risk(cls, v: str) -> str:
        norm = v.strip().lower() or RiskClass.PROPOSE.value
        if norm not in _ALLOWED_RISK:
            return RiskClass.PROPOSE.value
        return norm

    @field_validator("evidence_ids")
    @classmethod
    def _trim_evidence(cls, v: list[str]) -> list[str]:
        cleaned = [s for e in v if (s := str(e).strip())]
        return cleaned[:DEFAULT_MAX_EVIDENCE_PER_OBS]


class _HeartbeatResponse(BaseModel):
    """Top-level adapter response envelope."""

    observations: list[_Observation] = Field(default_factory=list)


class AutonomyHeartbeatJob(BaseJob):
    uses_llm = True
    default_model_role = "autonomy_heartbeat"

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            cursor_path = _resolve_cursor_path(ctx)
            seen_path = _resolve_seen_path(ctx)
            cursor = _read_cursor(cursor_path)
            now = ctx.fired_at

            event_rows = _collect_event_rows(ctx, since_iso=cursor.get("last_event_ts"))
            memory_rows = _collect_memory_rows(ctx, since_iso=cursor.get("last_memory_ts"))
            presence = _resolve_presence(ctx.app)

            if not event_rows and not memory_rows:
                # Idle short-circuit — never bill the model. Cursor is
                # already up-to-date by construction.
                return _ok(
                    ctx, t0,
                    detail="idle",
                    payload={"events": 0, "memory_writes": 0, "observations": 0},
                )

            chain = build_chain_for_job(
                ctx,
                default_role=AutonomyHeartbeatJob.default_model_role,
                log_label="autonomy_heartbeat",
            )
            if not chain:
                # Advance cursor anyway so we don't replay the same window
                # forever waiting on a role that may never come back.
                _advance_cursor(cursor_path, event_rows, memory_rows, cursor)
                return _ok(
                    ctx, t0,
                    detail="role_unavailable",
                    payload={
                        "events": len(event_rows),
                        "memory_writes": len(memory_rows),
                        "observations": 0,
                    },
                )

            prompt = _build_prompt(event_rows, memory_rows, presence)
            raw = await _call_with_fallback(prompt, chain, DEFAULT_TIMEOUT_S)
            parsed = _parse_response(raw)

            seen = _read_seen(seen_path, now=now, window_hours=DEFAULT_DEDUPE_WINDOW_HOURS)
            accepted: list[_Observation] = []
            for obs in parsed.observations[:DEFAULT_MAX_OBSERVATIONS]:
                key = _dedupe_key(obs.observation)
                if key in seen:
                    continue
                accepted.append(obs)
                seen.add(key)

            store = _resolve_memory_store(ctx.app)
            published = 0
            written = 0
            written_ids: list[str] = []
            for obs in accepted:
                memory_id = _write_memory(store, obs, when=now) if store is not None else None
                if memory_id is not None:
                    written += 1
                    written_ids.append(memory_id)
                # Publish to bus even when memory write failed — the
                # observation still surfaces to the kernel, which is the
                # whole point. The mapper consults the memory store via
                # memory_id only when present.
                _publish(obs, memory_id=memory_id, when=now)
                published += 1
                _append_seen(seen_path, key=_dedupe_key(obs.observation), when=now)

            _advance_cursor(cursor_path, event_rows, memory_rows, cursor)

            detail = (
                f"events={len(event_rows)} memwrites={len(memory_rows)} "
                f"obs_returned={len(parsed.observations)} accepted={len(accepted)} "
                f"published={published} memory_written={written}"
            )
            return _ok(
                ctx, t0,
                detail=detail,
                payload={
                    "events": len(event_rows),
                    "memory_writes": len(memory_rows),
                    "observations_returned": len(parsed.observations),
                    "observations_accepted": len(accepted),
                    "published": published,
                    "memory_written": written,
                    "memory_ids": written_ids,
                },
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("autonomy_heartbeat crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )


def _ok(ctx: JobContext, t0: float, *, detail: str, payload: dict) -> JobResult:
    return JobResult(
        job_name=ctx.job_name,
        run_id=ctx.run_id,
        ok=True,
        detail=detail,
        payload=payload,
        duration_ms=(time.monotonic() - t0) * 1000.0,
    )


# ── Inputs ──────────────────────────────────────────────────────────


def _collect_event_rows(ctx: JobContext, *, since_iso: str | None) -> list[dict[str, Any]]:
    """Return up to ``DEFAULT_INPUT_EVENT_CAP`` workspace events newer than ``since_iso``.

    Reads the live ``EventStore`` from the app. Falls back to an empty
    list when the store isn't mounted (CLI-only scheduler runs)."""
    store = _resolve_event_store(ctx.app)
    if store is None:
        return []
    try:
        events = store.list_events(limit=DEFAULT_INPUT_EVENT_CAP * 2)
    except Exception:
        log.exception("autonomy_heartbeat: list_events failed")
        return []
    rows: list[dict[str, Any]] = []
    for ev in events:
        ts = getattr(ev, "ts", "") or ""
        if since_iso and ts <= since_iso:
            continue
        rows.append({
            "event_id": getattr(ev, "event_id", ""),
            "ts": ts,
            "kind": getattr(ev, "kind", ""),
            "source": getattr(ev, "source", ""),
            "title": (getattr(ev, "title", "") or "")[:200],
            "summary": (getattr(ev, "summary", "") or "")[:400],
        })
        if len(rows) >= DEFAULT_INPUT_EVENT_CAP:
            break
    return rows


def _collect_memory_rows(ctx: JobContext, *, since_iso: str | None) -> list[dict[str, Any]]:
    """Return memory writes newer than ``since_iso`` from ``events/writes.jsonl``.

    Reads only ``status: written`` rows; ignores ``blocked``/``updated``."""
    store_dir = _resolve_memory_store_dir(ctx)
    if store_dir is None:
        return []
    writes_path = store_dir / "events" / "writes.jsonl"
    if not writes_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with writes_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("status") != "written":
                    continue
                ts = str(row.get("timestamp") or "")
                if since_iso and ts <= since_iso:
                    continue
                title = (str(row.get("title") or ""))[:200]
                # Self-amplification guard: the heartbeat writes its
                # observations to memory-store/conscience/autonomy/ with
                # titles prefixed `autonomy heartbeat — `. Excluding
                # those rows from the next tick's inputs prevents the
                # heartbeat from observing itself observing itself.
                if title.startswith("autonomy heartbeat"):
                    continue
                rows.append({
                    "memory_id": str(row.get("memory_id") or ""),
                    "ts": ts,
                    "type": str(row.get("type") or ""),
                    "title": title,
                })
    except OSError:
        log.exception("autonomy_heartbeat: writes.jsonl read failed")
        return []
    return rows[-DEFAULT_INPUT_MEMORY_CAP:]


def _resolve_presence(app: Any) -> dict[str, Any] | None:
    """Best-effort read of AU-21's operator presence cache. ``None`` when
    AU-21 isn't running or the cache is empty."""
    if app is None or not hasattr(app, "get"):
        return None
    cache = app.get("operator_presence")
    if cache is None:
        return None
    # The AU-21 cache is a dict-like; serialise the most operator-useful
    # fields and stay defensive about shape.
    try:
        return {
            "current_view": cache.get("current_view"),
            "dwell_seconds": cache.get("dwell_seconds"),
            "switches_today": cache.get("switches_today"),
        }
    except AttributeError:
        return None


# ── Adapter call ────────────────────────────────────────────────────


def _build_prompt(
    events: list[dict[str, Any]],
    memory_writes: list[dict[str, Any]],
    presence: dict[str, Any] | None,
) -> str:
    parts: list[str] = [
        "You are TARS's autonomy heartbeat — a periodic self-reflection pass.",
        "",
        "Below is recent activity since the last heartbeat. Your job is to",
        "spot at most 3 NOTABLE patterns that warrant a future agenda item",
        "(autonomous follow-up, a propose-class change, or operator-gated work).",
        "",
        "If nothing notable surfaced, return an empty observations array.",
        "Stay literal — do NOT invent activity not represented below.",
        "",
        "RESPONSE FORMAT (JSON object, no preamble):",
        "{\"observations\": [",
        "  {",
        "    \"observation\": \"<one sentence, 10-600 chars>\",",
        f"    \"suggested_risk_class\": \"<one of: {', '.join(sorted(_ALLOWED_RISK))}>\",",
        "    \"evidence_ids\": [\"evt_...\", \"mem_...\"]",
        "  }",
        "]}",
        "",
        "--- RECENT WORKSPACE EVENTS ---",
    ]
    if events:
        for ev in events:
            parts.append(
                f"- [{ev['ts']}] {ev['event_id']} kind={ev['kind']} "
                f"source={ev['source']} title=\"{ev['title']}\"\n"
                f"    {ev['summary']}"
            )
    else:
        parts.append("(none)")
    parts.extend(["", "--- RECENT MEMORY WRITES ---"])
    if memory_writes:
        for mw in memory_writes:
            parts.append(
                f"- [{mw['ts']}] {mw['memory_id']} type={mw['type']} title=\"{mw['title']}\""
            )
    else:
        parts.append("(none)")
    if presence is not None:
        parts.extend([
            "",
            "--- OPERATOR PRESENCE ---",
            f"current_view={presence.get('current_view')} "
            f"dwell_seconds={presence.get('dwell_seconds')} "
            f"switches_today={presence.get('switches_today')}",
        ])
    parts.extend(["", "Return the JSON object now."])
    return "\n".join(parts)


async def _call_with_fallback(
    prompt: str,
    chain: list[tuple[ModelAdapter, AdapterOptions]],
    timeout_s: float,
) -> str:
    for adapter, options in chain:
        label = f"{options.provider or '?'}/{options.model or '?'}"
        try:
            out = await asyncio.wait_for(
                adapter.generate(prompt, options),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            log.warning("autonomy_heartbeat: %s timed out after %.1fs", label, timeout_s)
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning("autonomy_heartbeat: %s call failed (%s)", label, exc)
            continue
        if out and out.strip():
            return out
        log.warning("autonomy_heartbeat: %s returned empty", label)
    return ""


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_response(raw: str) -> _HeartbeatResponse:
    """Extract + validate the observations envelope. Returns an empty
    envelope on any parse / validation failure so the job can still
    advance the cursor."""
    if not raw or not raw.strip():
        return _HeartbeatResponse()
    match = _JSON_OBJECT_RE.search(raw)
    if match is None:
        log.warning("autonomy_heartbeat: no JSON object in adapter output")
        return _HeartbeatResponse()
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        log.warning("autonomy_heartbeat: JSON parse failed")
        return _HeartbeatResponse()
    if not isinstance(data, dict):
        return _HeartbeatResponse()
    try:
        return _HeartbeatResponse.model_validate(data)
    except ValidationError as exc:
        # Try lenient: drop bad observations one by one.
        items = data.get("observations") or []
        if not isinstance(items, list):
            return _HeartbeatResponse()
        kept: list[_Observation] = []
        for item in items:
            try:
                kept.append(_Observation.model_validate(item))
            except ValidationError:
                continue
        if not kept:
            log.warning("autonomy_heartbeat: response failed validation: %s", exc)
        return _HeartbeatResponse(observations=kept)


# ── Memory write + bus publish ──────────────────────────────────────


def _resolve_memory_store(app: Any) -> Any:
    if app is None or not hasattr(app, "get"):
        return None
    bundle = app.get("memory_bundle")
    if bundle is None:
        return None
    return getattr(bundle, "store", None)


def _write_memory(store: Any, obs: _Observation, *, when: datetime) -> str | None:
    mem_id = MemoryFrontmatter.generate_id()
    title = f"autonomy heartbeat — {obs.observation[:80].strip()}"
    body = _format_memory_body(obs, when)
    fm = MemoryFrontmatter(
        id=mem_id,
        type=MemoryType.CONSCIENCE,
        title=title[:200],
        summary=obs.observation[:280],
        created_at=when,
        updated_at=when,
        importance=5,
        tags=[HEARTBEAT_TAG, obs.suggested_risk_class],
        stability=Stability.ACTIVE,
        source_type="autonomy_heartbeat",
    )
    try:
        ok = store.write(fm, body, subdir_override=CONSCIENCE_SUBDIR)
    except Exception:
        log.exception("autonomy_heartbeat: memory write raised")
        return None
    if not ok:
        log.info("autonomy_heartbeat: memory write declined by store")
        return None
    return mem_id


def _format_memory_body(obs: _Observation, when: datetime) -> str:
    lines = [
        "# Autonomy heartbeat observation",
        "",
        f"- emitted_at: {when.isoformat()}",
        f"- suggested_risk_class: {obs.suggested_risk_class}",
        "",
        "## Observation",
        "",
        obs.observation.strip(),
    ]
    if obs.evidence_ids:
        lines.extend(["", "## Evidence", ""])
        for ev_id in obs.evidence_ids:
            lines.append(f"- {ev_id}")
    return "\n".join(lines)[:DEFAULT_BODY_MAX_CHARS]


def _publish(obs: _Observation, *, memory_id: str | None, when: datetime) -> None:
    payload = {
        "observation": obs.observation,
        "suggested_risk_class": obs.suggested_risk_class,
        "evidence_ids": list(obs.evidence_ids),
        "memory_id": memory_id,
        "emitted_at": when.isoformat(),
        "source_handler": "autonomy_heartbeat",
    }
    # event_id keyed off the observation hash so a replay of an
    # already-known observation collides on the mapper's dedupe.
    event_id = f"evt_heartbeat_{_dedupe_key(obs.observation)[:16]}"
    publish_to_bus(AgendaSource.SELF_REFLECTION, payload, event_id=event_id)


# ── Dedup + cursor ──────────────────────────────────────────────────


def _dedupe_key(observation: str) -> str:
    normalised = " ".join(observation.lower().split())
    return hashlib.sha1(normalised.encode("utf-8")).hexdigest()


def _read_seen(path: Path, *, now: datetime, window_hours: int) -> set[str]:
    if not path.exists():
        return set()
    cutoff = now - timedelta(hours=window_hours)
    cutoff_iso = cutoff.isoformat()
    keep: set[str] = set()
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
                ts = str(row.get("ts") or "")
                if ts < cutoff_iso:
                    continue
                key = str(row.get("key") or "")
                if key:
                    keep.add(key)
    except OSError:
        log.exception("autonomy_heartbeat: seen read failed")
        return set()
    return keep


def _append_seen(path: Path, *, key: str, when: datetime) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"key": key, "ts": when.isoformat()}) + "\n")
    except OSError:
        log.exception("autonomy_heartbeat: seen append failed")


def _read_cursor(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _advance_cursor(
    path: Path,
    events: Iterable[dict[str, Any]],
    memory_writes: Iterable[dict[str, Any]],
    prior: dict[str, str],
) -> None:
    """Persist the maximum ``ts`` seen for events / memory writes.

    Falls back to the prior cursor value when a feed is empty so a
    quiet feed never erases its watermark."""
    event_max = max((str(e.get("ts") or "") for e in events), default="") or prior.get("last_event_ts", "")
    mem_max = max((str(m.get("ts") or "") for m in memory_writes), default="") or prior.get("last_memory_ts", "")
    payload = {"last_event_ts": event_max, "last_memory_ts": mem_max}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        log.exception("autonomy_heartbeat: cursor write failed")


# ── Path + store resolution ─────────────────────────────────────────


def _autonomy_dir() -> Path:
    override = os.environ.get("TESSERACT_HOME")
    home = Path(override).resolve() if override else TESSERACT_HOME
    return home / "autonomy"


def _resolve_cursor_path(ctx: JobContext) -> Path:
    override = ctx.config.get("cursor_path")
    if override:
        return Path(override)
    return _autonomy_dir() / "heartbeat-cursor.json"


def _resolve_seen_path(ctx: JobContext) -> Path:
    override = ctx.config.get("seen_path")
    if override:
        return Path(override)
    return _autonomy_dir() / "heartbeat-seen.jsonl"


def _resolve_event_store(app: Any) -> Any:
    if app is None or not hasattr(app, "get"):
        return None
    return app.get("workspace_event_store")


def _resolve_memory_store_dir(ctx: JobContext) -> Path | None:
    override = ctx.config.get("memory_store_dir")
    if override:
        return Path(override)
    app = ctx.app
    if app is not None and hasattr(app, "get"):
        tdir = app.get("tesseract_dir")
        if tdir is not None:
            return Path(tdir) / "memory-store"
        bundle = app.get("memory_bundle")
        store = getattr(bundle, "store", None)
        store_dir = getattr(store, "store_dir", None)
        if store_dir is not None:
            return Path(store_dir)
    # Call-time env read — matches `_autonomy_dir`. A test that
    # monkeypatches TESSERACT_HOME after import (the autouse
    # `_isolate_tesseract_home` fixture) must not silently fall through
    # to the production memory-store.
    override = os.environ.get("TESSERACT_HOME")
    home = Path(override).resolve() if override else TESSERACT_HOME
    return home / "memory-store"


__all__ = ["AutonomyHeartbeatJob"]
