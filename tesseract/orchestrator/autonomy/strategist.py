"""AU-23 — autonomy strategist primitives.

The strategist is a low-cadence LLM job that produces 1-3 high-conviction
initiatives per scheduled tick. It is the missing seam between the
autonomy heartbeat (which NOTICES per-tick) and the operator (who
CHOOSES). This module hosts the pure pieces — Pydantic model, input
collection, prompt rendering, parsing, dedup ledger — so the scheduler
job (`tesseract/scheduler/tasks/autonomy_strategist.py`) and the mapper
(`tesseract/orchestrator/autonomy/mappers/strategist.py`) stay thin.

Cadence is NOT hardcoded here — `schedule.yaml::autonomy_strategist.cadence`
is the single source of truth, and the job passes `lookback_days` /
`dedupe_window_days` overrides through `ctx.config` so changing the cron
expression doesn't require code edits.

Distinct from the AU-20 heartbeat:
- heartbeat = reactive, per-tick (every few minutes), agenda-candidate
  level (one observation).
- strategist = deliberate, low-cadence (configurable, days-scale),
  portfolio level (1-3 initiatives with explicit success criteria +
  horizon_days).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from tesseract.orchestrator.autonomy.models import (
    AgendaSource,
    AgendaStatus,
    RiskClass,
)
from tesseract.paths import TESSERACT_HOME

log = logging.getLogger(__name__)


# ── Defaults ────────────────────────────────────────────────────────

DEFAULT_MAX_INITIATIVES = 3
DEFAULT_MIN_CONFIDENCE = 0.6
DEFAULT_HORIZON_DAYS = 7
DEFAULT_DEDUPE_WINDOW_DAYS = 14
DEFAULT_IDLE_LOOKBACK_DAYS = 7

# Input window caps — the strategist sees up to N rows from each substrate
# so the prompt stays bounded even on a busy week.
DEFAULT_AGENDA_CAP = 30
DEFAULT_LEAF_CAP = 30
DEFAULT_VAULT_CAP = 15
DEFAULT_WORKER_FAIL_CAP = 15
DEFAULT_PAUSE_CAP = 10

_ALLOWED_RISK = frozenset({RiskClass.PROPOSE.value, RiskClass.OPERATOR_GATE.value})
# Strategist initiatives are by design operator-attended. AUTONOMOUS is
# disallowed (would bypass operator approval) and ABSOLUTE_DENY is
# nonsensical here (the strategist proposes, doesn't ban); both are
# coerced to PROPOSE in `_coerce_risk`. The mapper additionally attaches
# an operator_review gate regardless of risk_class.

_SLUG_RE = re.compile(r"[^a-z0-9-]+")


# ── Initiative model ────────────────────────────────────────────────


class Initiative(BaseModel):
    """One high-conviction portfolio initiative — strategist's output unit."""

    model_config = ConfigDict(frozen=False, extra="forbid")

    slug: str = Field(min_length=1, max_length=40)
    goal: str = Field(min_length=10, max_length=500)
    rationale: str = Field(min_length=10, max_length=2000)
    success_criteria: list[str] = Field(default_factory=list)
    suggested_risk_class: RiskClass = RiskClass.PROPOSE
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    horizon_days: int = Field(default=DEFAULT_HORIZON_DAYS, ge=1, le=90)

    @field_validator("slug", mode="before")
    @classmethod
    def _normalise_slug(cls, v: Any) -> str:
        raw = str(v or "").strip().lower()
        cleaned = _SLUG_RE.sub("-", raw)
        cleaned = "-".join(filter(None, cleaned.split("-")))
        return cleaned[:40] or "initiative"

    @field_validator("success_criteria")
    @classmethod
    def _trim_criteria(cls, v: list[str]) -> list[str]:
        kept: list[str] = []
        for entry in v or []:
            text = str(entry).strip()
            if text:
                kept.append(text[:300])
        if not kept:
            raise ValueError("success_criteria must have at least one entry")
        return kept[:3]

    @field_validator("evidence")
    @classmethod
    def _trim_evidence(cls, v: list[str]) -> list[str]:
        kept = [str(e).strip() for e in (v or []) if str(e).strip()]
        return kept[:8]

    @field_validator("suggested_risk_class", mode="before")
    @classmethod
    def _coerce_risk(cls, v: Any) -> Any:
        if isinstance(v, RiskClass):
            return v
        norm = str(v or "").strip().lower() or RiskClass.PROPOSE.value
        if norm not in _ALLOWED_RISK:
            # Catches AUTONOMOUS, ABSOLUTE_DENY, and any unrecognised value.
            return RiskClass.PROPOSE
        return RiskClass(norm)


class _InitiativeEnvelope(BaseModel):
    """Top-level adapter response envelope."""

    initiatives: list[Initiative] = Field(default_factory=list)


# ── Input collection ────────────────────────────────────────────────


@dataclass(frozen=True)
class StrategistInputs:
    """Pre-fetched substrate the strategist sees. Empty lists in every
    field → idle short-circuit (caller skips the LLM call)."""

    agenda_recent: list[dict[str, Any]]
    discovery_leaves: list[dict[str, Any]]
    vault_deltas: list[dict[str, Any]]
    failed_workers: list[dict[str, Any]]
    paused_sources: list[dict[str, Any]]
    presence: dict[str, Any] | None
    window_start_iso: str
    window_end_iso: str

    def is_idle(self) -> bool:
        """True when no fresh signal landed in the window.

        Deliberately excludes ``paused_sources`` and ``presence`` —
        those are *context* the model uses to interpret signal, not
        signal themselves. A tick where the only thing happening is
        "the governor paused observer 12 days ago and nothing else
        moved" is correctly idle: the pause was already surfaced when
        it happened, and re-proposing on every quiet tick would spam.
        Tonight's worker failures + this week's discovery leaves are
        what drive `is_idle()`.
        """
        return not (
            self.agenda_recent
            or self.discovery_leaves
            or self.vault_deltas
            or self.failed_workers
        )


def collect_inputs(
    *,
    app: Any,
    now: datetime,
    lookback_days: int = DEFAULT_IDLE_LOOKBACK_DAYS,
    tesseract_home: Path | None = None,
) -> StrategistInputs:
    """Pure-read pre-fetcher. Surface failures as empty lists — the
    strategist's idle short-circuit is the right response to "everything
    is empty," whether the source is genuinely quiet or temporarily
    unreadable."""
    window_start = now - timedelta(days=lookback_days)
    home = _resolve_home(tesseract_home)

    return StrategistInputs(
        agenda_recent=_collect_agenda(home, window_start=window_start),
        discovery_leaves=_collect_leaves(home, window_start=window_start),
        vault_deltas=_collect_vault_deltas(home, window_start=window_start),
        failed_workers=_collect_failed_workers(home, window_start=window_start),
        paused_sources=_collect_paused_sources(home),
        presence=_collect_presence(app),
        window_start_iso=window_start.isoformat(),
        window_end_iso=now.isoformat(),
    )


def _resolve_home(override: Path | None) -> Path:
    """Call-time resolution. Mirrors `autonomy/paths.py::_home` and the
    workspace_changes pattern — never capture the import-time constant
    so tests that monkeypatch `TESSERACT_HOME` mid-run don't fall
    through to the source-tree fallback."""
    if override is not None:
        return Path(override)
    env_override = os.environ.get("TESSERACT_HOME")
    if env_override:
        return Path(env_override).resolve()
    return TESSERACT_HOME


def _collect_agenda(home: Path, *, window_start: datetime) -> list[dict[str, Any]]:
    """Recent agenda transitions from ``agenda/index.jsonl``.

    Reads the audit log instead of scanning ``active/`` because the
    strategist cares about OUTCOMES (DONE / CANCELLED / ABANDONED /
    BLOCKED), not currently-pending items. Index is bounded; reading
    the tail is cheap."""
    path = home / "agenda" / "index.jsonl"
    if not path.exists():
        return []
    cutoff = window_start.isoformat()
    rows: list[dict[str, Any]] = []
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
                if not ts or ts < cutoff:
                    continue
                rows.append({
                    "id": str(row.get("item_id") or ""),
                    "ts": ts,
                    "to_status": str(row.get("to_status") or ""),
                    "from_status": str(row.get("from_status") or ""),
                    "reason": str(row.get("reason") or "")[:200],
                    "goal": str(row.get("goal") or "")[:200],
                    "source": str(row.get("source") or ""),
                })
    except OSError:
        log.exception("strategist: agenda index read failed")
        return []
    return rows[-DEFAULT_AGENDA_CAP:]


def _collect_leaves(home: Path, *, window_start: datetime) -> list[dict[str, Any]]:
    """AU-16 discovery leaves from ``memory-store/leaves/index.jsonl``.

    The index has one row per state transition; we read the tail and
    keep only ``state in {admitted, buffered, sealed}`` rows so the
    strategist sees real signal — pending leaves haven't passed the
    admission gate yet, dropped leaves were rejected as noise."""
    path = home / "memory-store" / "leaves" / "index.jsonl"
    if not path.exists():
        return []
    cutoff = window_start.isoformat()
    seen_ids: set[str] = set()
    rows: list[dict[str, Any]] = []
    keep_states = {"admitted", "buffered", "sealed"}
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
                ts = str(row.get("ts") or row.get("created_at") or "")
                if not ts or ts < cutoff:
                    continue
                state = str(row.get("state") or row.get("to_state") or "").lower()
                if state not in keep_states:
                    continue
                leaf_id = str(row.get("leaf_id") or row.get("id") or "")
                if leaf_id and leaf_id in seen_ids:
                    continue
                if leaf_id:
                    seen_ids.add(leaf_id)
                rows.append({
                    "leaf_id": leaf_id,
                    "ts": ts,
                    "state": state,
                    "source": str(row.get("source") or "")[:60],
                    "title": str(row.get("title") or "")[:200],
                })
    except OSError:
        log.exception("strategist: leaf index read failed")
        return []
    return rows[-DEFAULT_LEAF_CAP:]


def _collect_vault_deltas(home: Path, *, window_start: datetime) -> list[dict[str, Any]]:
    """Wiki ingest log rows newer than ``window_start``."""
    path = home / "vault" / "wiki" / "ingest-log.md"
    if not path.exists():
        return []
    cutoff_date = window_start.date().isoformat()
    rows: list[dict[str, Any]] = []
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line.startswith("- "):
                continue
            # Conservative parse: line shape is `- YYYY-MM-DD HH:MM — kind — title`.
            # We only need a date stamp + a short preview.
            head = line[2:].split(" — ", 1)
            stamp = head[0].strip()
            if len(stamp) < 10 or stamp[:10] < cutoff_date:
                continue
            rest = head[1] if len(head) == 2 else ""
            rows.append({
                "ts": stamp[:19],
                "preview": rest[:200],
            })
    except OSError:
        log.exception("strategist: vault ingest log read failed")
        return []
    return rows[-DEFAULT_VAULT_CAP:]


def _collect_failed_workers(home: Path, *, window_start: datetime) -> list[dict[str, Any]]:
    """Worker records that ended in FAILED / DENIED inside the window."""
    base = home / "workers"
    if not base.exists():
        return []
    cutoff = window_start.isoformat()
    fails: list[dict[str, Any]] = []
    failure_states = {"failed", "denied", "timeout"}
    try:
        for path in sorted(base.rglob("*.json")):
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            status = str(row.get("status") or "").lower()
            if status not in failure_states:
                continue
            ts = str(row.get("updated_at") or row.get("created_at") or "")
            if not ts or ts < cutoff:
                continue
            fails.append({
                "worker_id": str(row.get("worker_id") or row.get("id") or ""),
                "ts": ts,
                "kind": str(row.get("kind") or ""),
                "status": status,
                "goal": str(row.get("goal") or row.get("summary") or "")[:200],
            })
            if len(fails) >= DEFAULT_WORKER_FAIL_CAP * 2:
                break
    except OSError:
        log.exception("strategist: worker records walk failed")
        return []
    fails.sort(key=lambda r: r["ts"], reverse=True)
    return fails[:DEFAULT_WORKER_FAIL_CAP]


def _collect_paused_sources(home: Path) -> list[dict[str, Any]]:
    """Governor pauses snapshot."""
    path = home / "autonomy" / "governor-paused.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        for source, body in payload.items():
            if not isinstance(body, dict):
                continue
            rows.append({
                "source": str(source),
                "reason": str(body.get("reason") or "")[:200],
                "since": str(body.get("paused_at") or body.get("since") or ""),
            })
    return rows[:DEFAULT_PAUSE_CAP]


def _collect_presence(app: Any) -> dict[str, Any] | None:
    if app is None or not hasattr(app, "get"):
        return None
    cache = app.get("operator_presence")
    if cache is None:
        return None
    try:
        return {
            "current_view": cache.get("current_view"),
            "dwell_seconds": cache.get("dwell_seconds"),
            "switches_today": cache.get("switches_today"),
        }
    except AttributeError:
        return None


# ── Prompt ──────────────────────────────────────────────────────────


def build_prompt(inputs: StrategistInputs) -> str:
    parts: list[str] = [
        "You are the assistant's autonomy strategist — a low-cadence portfolio curator.",
        "",
        "Below is the activity that landed inside the lookback window shown",
        f"in the Window: header. Your job is to choose AT MOST {DEFAULT_MAX_INITIATIVES}",
        "high-conviction initiatives the assistant should pursue between now and the",
        "next scheduled tick. Initiatives are operator-attended — every one",
        "ships with an operator_review gate, so the operator decides whether",
        "to accept.",
        "",
        "Be ruthless. Most ticks deserve 1-2 initiatives, not 3. If nothing",
        "rises above noise, return an empty initiatives array. Quality over",
        "quantity is the contract.",
        "",
        f"Window: {inputs.window_start_iso} → {inputs.window_end_iso}",
        "",
        "RESPONSE FORMAT (JSON object, no preamble, no code fence):",
        "{\"initiatives\": [",
        "  {",
        "    \"slug\": \"<short kebab-case identifier, ≤40 chars>\",",
        "    \"goal\": \"<imperative one-sentence target, 10-500 chars>\",",
        "    \"rationale\": \"<why this, why now, 10-2000 chars>\",",
        "    \"success_criteria\": [\"<concrete check 1>\", \"<concrete check 2>\"],",
        f"    \"suggested_risk_class\": \"<one of: {', '.join(sorted(_ALLOWED_RISK))}>\",",
        "    \"evidence\": [\"<agenda id / leaf id / vault preview / worker id>\"],",
        "    \"confidence\": <float 0.0-1.0; ≥0.6 will be kept>,",
        "    \"horizon_days\": <int 1-90; how long this stays fresh>",
        "  }",
        "]}",
        "",
        "--- RECENT AGENDA OUTCOMES ---",
    ]
    if inputs.agenda_recent:
        for row in inputs.agenda_recent:
            parts.append(
                f"- [{row['ts']}] {row['id']} {row['from_status']}→{row['to_status']} "
                f"source={row['source']} goal=\"{row['goal']}\" reason=\"{row['reason']}\""
            )
    else:
        parts.append("(none)")

    parts.extend(["", "--- DISCOVERY LEAVES ---"])
    if inputs.discovery_leaves:
        for row in inputs.discovery_leaves:
            parts.append(
                f"- [{row['ts']}] {row['leaf_id']} state={row['state']} "
                f"source={row['source']} title=\"{row['title']}\""
            )
    else:
        parts.append("(none)")

    parts.extend(["", "--- VAULT DELTAS ---"])
    if inputs.vault_deltas:
        for row in inputs.vault_deltas:
            parts.append(f"- [{row['ts']}] {row['preview']}")
    else:
        parts.append("(none)")

    parts.extend(["", "--- WORKER FAILURES ---"])
    if inputs.failed_workers:
        for row in inputs.failed_workers:
            parts.append(
                f"- [{row['ts']}] {row['worker_id']} kind={row['kind']} "
                f"status={row['status']} goal=\"{row['goal']}\""
            )
    else:
        parts.append("(none)")

    parts.extend(["", "--- PAUSED SOURCES (governor) ---"])
    if inputs.paused_sources:
        for row in inputs.paused_sources:
            parts.append(
                f"- {row['source']} since={row['since']} reason=\"{row['reason']}\""
            )
    else:
        parts.append("(none)")

    if inputs.presence is not None:
        parts.extend([
            "",
            "--- OPERATOR PRESENCE ---",
            f"current_view={inputs.presence.get('current_view')} "
            f"dwell_seconds={inputs.presence.get('dwell_seconds')} "
            f"switches_today={inputs.presence.get('switches_today')}",
        ])

    parts.extend(["", "Return the JSON object now."])
    return "\n".join(parts)


# ── Parsing ─────────────────────────────────────────────────────────


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_response(raw: str) -> list[Initiative]:
    """Lenient parse — drops bad initiatives one by one rather than
    rejecting the whole batch, mirroring AU-20's parser."""
    if not raw or not raw.strip():
        return []
    match = _JSON_OBJECT_RE.search(raw)
    if match is None:
        log.warning("strategist: no JSON object in adapter output")
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        log.warning("strategist: JSON parse failed")
        return []
    if not isinstance(data, dict):
        return []
    try:
        envelope = _InitiativeEnvelope.model_validate(data)
        return envelope.initiatives
    except ValidationError:
        items = data.get("initiatives") or []
        if not isinstance(items, list):
            return []
        kept: list[Initiative] = []
        for entry in items:
            try:
                kept.append(Initiative.model_validate(entry))
            except ValidationError:
                continue
        return kept


def filter_initiatives(
    initiatives: Iterable[Initiative],
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    max_count: int = DEFAULT_MAX_INITIATIVES,
) -> list[Initiative]:
    """Drop low-confidence rows, cap to ``max_count`` keeping the highest
    confidence first. Deterministic — same inputs always produce the same
    output."""
    pool = [i for i in initiatives if i.confidence >= min_confidence]
    pool.sort(key=lambda i: (-i.confidence, i.slug))
    return pool[:max_count]


# ── Dedup ledger ────────────────────────────────────────────────────


def goal_key(goal: str) -> str:
    """SHA-1 of the normalised goal — single source of truth for the
    strategist dedup contract. Reaper / mapper / tests must call this
    helper (don't re-implement the hash; a silent normalisation drift
    breaks the join between ledger and agenda items)."""
    normalised = " ".join(goal.lower().split())
    return hashlib.sha1(normalised.encode("utf-8")).hexdigest()


def initiative_key(initiative: Initiative) -> str:
    """Convenience wrapper — slug is human-friendly, goal is the dedup
    anchor (operator wording shouldn't fork ledgers)."""
    return goal_key(initiative.goal)


def read_seen(
    path: Path,
    *,
    now: datetime,
    window_days: int = DEFAULT_DEDUPE_WINDOW_DAYS,
) -> set[str]:
    if not path.exists():
        return set()
    cutoff = now - timedelta(days=window_days)
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
        log.exception("strategist: seen ledger read failed")
        return set()
    return keep


def append_seen(
    path: Path,
    *,
    initiative: Initiative,
    when: datetime,
) -> bool:
    """Append one row to the dedup ledger. Returns ``True`` on confirmed
    write, ``False`` when the write failed.

    The caller MUST gate the publish on the return value — emitting a
    bus event without a confirmed ledger row would let the same
    initiative re-fire on every tick of the dedup window. AU-23 phase
    contract: dedup is a hard invariant, not a best-effort hint.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({
                    "key": initiative_key(initiative),
                    "slug": initiative.slug,
                    "goal": initiative.goal,
                    "horizon_days": initiative.horizon_days,
                    "ts": when.isoformat(),
                }) + "\n"
            )
    except OSError:
        log.exception("strategist: seen ledger append failed")
        return False
    return True


def dedupe_against_ledger(
    initiatives: Iterable[Initiative],
    *,
    seen: set[str],
) -> list[Initiative]:
    """Return only initiatives whose goal hash isn't in ``seen``."""
    fresh: list[Initiative] = []
    for initiative in initiatives:
        if initiative_key(initiative) in seen:
            continue
        fresh.append(initiative)
    return fresh


# ── Path helpers ────────────────────────────────────────────────────


def strategist_dir(home: Path | None = None) -> Path:
    return _resolve_home(home) / "autonomy"


def seen_ledger_path(home: Path | None = None) -> Path:
    return strategist_dir(home) / "strategist-seen.jsonl"


__all__ = [
    "DEFAULT_AGENDA_CAP",
    "DEFAULT_DEDUPE_WINDOW_DAYS",
    "DEFAULT_HORIZON_DAYS",
    "DEFAULT_IDLE_LOOKBACK_DAYS",
    "DEFAULT_LEAF_CAP",
    "DEFAULT_MAX_INITIATIVES",
    "DEFAULT_MIN_CONFIDENCE",
    "DEFAULT_PAUSE_CAP",
    "DEFAULT_VAULT_CAP",
    "DEFAULT_WORKER_FAIL_CAP",
    "Initiative",
    "StrategistInputs",
    "append_seen",
    "build_prompt",
    "collect_inputs",
    "dedupe_against_ledger",
    "filter_initiatives",
    "goal_key",
    "initiative_key",
    "parse_response",
    "read_seen",
    "seen_ledger_path",
    "strategist_dir",
]
