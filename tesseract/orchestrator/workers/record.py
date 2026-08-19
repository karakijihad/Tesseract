"""Durable ``WorkerRecord`` — Pydantic v2 shape + atomic IO.

A worker MUST have its record written to disk BEFORE the underlying
process / task starts, so recovery can always find it. Status
transitions go through ``write_record`` → atomic ``.tmp`` rewrite of
``record.json``. Per the GOVERNANCE rule "Restart-safe end-to-end",
ephemeral asyncio state without a disk record is a defect.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tesseract.orchestrator.outcome import RunOutcome
from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.paths import (
    worker_dir,
    workers_active_dir,
    workers_archive_dir,
)

log = logging.getLogger(__name__)


class RiskClass(str, Enum):
    """Four-tier risk taxonomy per ``_shared/risk-class-taxonomy.md``.
    Every agenda item and worker carries one; admission compares the
    item's class against the dispatched tool/worker's class and refuses
    if the item is more permissive than the tool allows."""

    AUTONOMOUS = "autonomous"
    PROPOSE = "propose"
    OPERATOR_GATE = "operator_gate"
    ABSOLUTE_DENY = "absolute_deny"


class WorkerStatus(str, Enum):
    """Lifecycle status. Terminal: done / failed / blocked / cancelled.
    ``interrupted`` is recovery-only: written by RecoveryManager when a
    running worker is found stale or PID-dead after a restart."""

    QUEUED = "queued"
    SPAWNING = "spawning"
    RUNNING = "running"
    AWAITING_IO = "awaiting_io"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class Billing(str, Enum):
    """How this worker's cost was billed.

    ``subscription`` covers the CLI workers (claude_cli / codex_cli /
    pty_*) — flat-rate Anthropic / OpenAI plans that don't surface
    per-call usage. ``tokens_in`` / ``tokens_out`` / ``cost_usd`` stay
    at 0 for these by design; the UI shows a ``sub`` badge instead of
    a fake ``$0.00`` so the operator isn't misled.

    ``api`` is for workers backed by the metered API (``invoke_agent``,
    ``agent_self`` — both hit the chat_brain via the Anthropic SDK).
    Per-call usage IS reportable; the runner copies it into the record.

    ``unknown`` is the cautious default when the kind hasn't declared a
    billing posture yet — never silently labelled as one or the other.
    """

    SUBSCRIPTION = "subscription"
    API = "api"
    UNKNOWN = "unknown"


TERMINAL_STATUSES = frozenset(
    {
        WorkerStatus.DONE,
        WorkerStatus.FAILED,
        WorkerStatus.BLOCKED,
        WorkerStatus.INTERRUPTED,
        WorkerStatus.CANCELLED,
    }
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ArtifactRef(BaseModel):
    """Pointer to an output file the worker produced. Stored at the
    worker dir's ``artifacts/`` subdir on terminal completion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    kind: str = "file"
    size_bytes: int | None = None
    sha256: str | None = None


class StatusTransition(BaseModel):
    """Append-only entry in ``status_history``. Lets the dashboard
    render the worker's timeline without re-deriving from event logs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    at: datetime
    from_status: str
    to_status: str
    reason: str = ""


class WorkerRecord(BaseModel):
    """Schema mirrors ``_shared/worker-record-schema.md``. ``frozen=False``
    because status transitions mutate the record in place before the
    atomic rewrite; ``extra="forbid"`` so a typo'd field is a load-time
    error, not silently dropped."""

    # ``validate_assignment`` so the outcome invariant below holds however the
    # field is set — the runner mutates a constructed record, which a plain
    # validator would never see.
    model_config = ConfigDict(frozen=False, extra="forbid", validate_assignment=True)

    # Identity
    id: str
    kind: WorkerKind
    created_at: datetime
    updated_at: datetime

    # Lineage
    agenda_item_id: str
    mission_id: str | None = None
    parent_worker_id: str | None = None

    # Posture
    risk_class: RiskClass
    role: str

    # Inputs
    prompt: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)
    worktree_path: str | None = None

    # Process / pane
    pid: int | None = None
    pane_id: str | None = None
    cli_invocation: list[str] | None = None

    # State
    status: WorkerStatus = WorkerStatus.QUEUED
    status_history: list[StatusTransition] = Field(default_factory=list)
    last_heartbeat: datetime | None = None
    exit_code: int | None = None
    error_class: str | None = None
    error_message: str | None = None

    # Budget
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    retry_count: int = 0
    billing: Billing = Billing.UNKNOWN

    # Outputs
    summary: str = ""
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    transcript_path: str | None = None

    # What came of the run, as opposed to where it ended up. ``status`` is
    # the lifecycle; ``outcome`` is the closed vocabulary in
    # ``orchestrator/outcome.py``, and a run that produced nothing may not
    # claim one of the healthy ones. ``None`` on records written before the
    # field existed — a reader must treat that as unknown, never as success.
    outcome: RunOutcome | None = None
    outcome_reason: str = ""

    # TC-4 — controller-attested workers (AGENT_CONTROLLER kind). All
    # nullable; existing kinds keep their semantics untouched. The
    # controller fills these on dispatch so recovery can REATTACH:
    # `controller_pid` alive + `controller_hb_path` fresh → reattach.
    controller_id: str | None = None
    controller_pid: int | None = None
    controller_hb_path: str | None = None
    session_id: str | None = None

    @model_validator(mode="after")
    def _outcome_carries_its_reason(self) -> "WorkerRecord":
        """Every non-`succeeded` outcome explains itself, whoever set it.

        An unexplained failure on a health surface is the same dead end as the
        empty success this vocabulary replaced. Enforced here rather than only
        in `set_outcome` so a direct assignment or a hand-built record cannot
        route around it — `validate_assignment` is what makes that reach the
        runner, which mutates an already-constructed record.
        """
        if self.outcome is not None and self.outcome is not RunOutcome.SUCCEEDED:
            if not self.outcome_reason.strip():
                raise ValueError(
                    f"worker {self.id}: outcome {self.outcome.value} needs a reason"
                )
        return self

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def set_outcome(self, outcome: RunOutcome, *, reason: str = "") -> None:
        """Record what came of the run, outcome and reason together.

        The pair is what the invariant is about, so setting them in one call
        avoids the transient state a two-step assignment would have to pass
        through.
        """
        if outcome is not RunOutcome.SUCCEEDED and not reason.strip():
            raise ValueError(
                f"worker {self.id}: outcome {outcome.value} needs a reason"
            )
        self.outcome_reason = reason
        self.outcome = outcome

    def transition_to(self, new_status: WorkerStatus, *, reason: str = "") -> None:
        """Mutate in place: append history entry, bump ``updated_at``,
        set ``status``. Caller must follow with ``write_record`` for the
        change to land on disk.

        Phase 7 (2026-05-22): when ``new_status`` is terminal, stamp
        ``duration_seconds`` from ``created_at`` so the operator sees how
        long the worker actually lived. Previously the field stayed at
        0.0 even for workers that ran for minutes — the autonomy
        dashboard couldn't show "this worker took 5 min before failing".
        """
        if new_status == self.status:
            return
        now = _utcnow()
        self.status_history.append(
            StatusTransition(
                at=now,
                from_status=self.status.value,
                to_status=new_status.value,
                reason=reason,
            )
        )
        self.status = new_status
        self.updated_at = now
        if new_status in TERMINAL_STATUSES and self.duration_seconds == 0.0:
            try:
                self.duration_seconds = max(
                    0.0, (now - self.created_at).total_seconds()
                )
            except Exception:
                # Defensive: a malformed created_at must not block the
                # status transition; leave duration at 0.0 and continue.
                pass


def mint_worker_id(kind: WorkerKind, *, now: datetime | None = None) -> str:
    """``wk-YYYY-MM-DD-HHMM-<kind>-<6 hex>``. Six-hex suffix prevents
    collisions across same-minute spawns; the kind segment keeps the id
    operator-readable when inspected by hand."""
    when = (now or _utcnow()).astimezone(timezone.utc)
    stamp = when.strftime("%Y-%m-%d-%H%M")
    return f"wk-{stamp}-{kind.value}-{secrets.token_hex(3)}"


def _record_path(worker_id: str) -> Path:
    return worker_dir(worker_id) / "record.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically: per-writer ``.tmp`` → ``os.replace`` →
    final path. The parent dir is created if missing; ``os.replace``
    is atomic on both Windows and POSIX for same-volume renames.

    The ``.tmp`` suffix is process+token unique (``<pid>.<6hex>.tmp``)
    so two concurrent writers targeting the same ``record.json`` —
    e.g. a status transition racing a heartbeat-driven update once
    AU-5 wires live runners — don't interleave bytes over a shared
    temp file. The reviewer flagged this race at AU-3 S1 review;
    we fix it before the blast radius grows.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f"{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp"
    )
    try:
        tmp.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except Exception:
        # Clean up the orphaned temp if the rename failed mid-flight.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def write_record(record: WorkerRecord) -> Path:
    """Atomic write of ``record.json``. Returns the resolved path. Safe
    to call repeatedly across status transitions — the record is the
    single source of truth, not the in-process object.

    Phase 3 (2026-05-22): after the disk write, fires a WS broadcast via
    ``workers/broadcast.py``. Event type is inferred from ``status_history``
    so callers don't need updating — a fresh record (one entry) emits
    ``worker_record_started``; subsequent writes emit
    ``worker_record_transitioned``. The hook is unset in REPL / standalone
    contexts so the broadcast is a no-op there.
    """
    path = _record_path(record.id)
    _atomic_write_json(path, record.model_dump(mode="json"))
    # Local import keeps `record.py` import-safe in contexts where the
    # broadcaster's optional aiohttp import would fail (e.g. CLI tooling).
    from tesseract.orchestrator.workers.broadcast import fire_worker_broadcast

    event = (
        "worker_record_started"
        if len(record.status_history) <= 1
        else "worker_record_transitioned"
    )
    fire_worker_broadcast(event, record)
    return path


def load_record(worker_id: str) -> WorkerRecord | None:
    """Read ``record.json`` for a worker. Returns ``None`` if absent;
    raises ``ValidationError`` if present-but-malformed (recovery
    catches that and flags the operator)."""
    path = _record_path(worker_id)
    if not path.exists():
        # Check archive bucket — terminal workers archive out of active/.
        archive = _find_in_archive(worker_id)
        if archive is None:
            return None
        path = archive
    raw = json.loads(path.read_text(encoding="utf-8"))
    return WorkerRecord.model_validate(raw)


def iter_active_status_summary() -> "Iterator[tuple[str, str, str]]":
    """Cheap scan of ``workers/active/`` yielding ``(worker_id, kind,
    status)`` tuples without Pydantic validation. Used by
    ``WorkerLane.running_count`` on the dispatch hot path — at 1000
    active workers a full ``list_active_records`` parse would cost 1000
    model validations per admission decision. This bypasses that.

    Malformed records (unreadable JSON, missing ``kind``/``status``)
    are skipped; full-fidelity reads land via ``list_active_records``.
    """
    root = workers_active_dir()
    if not root.exists():
        return
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        path = child / "record.json"
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            kind = raw.get("kind")
            status = raw.get("status")
            if not isinstance(kind, str) or not isinstance(status, str):
                continue
            yield child.name, kind, status
        except (OSError, ValueError):
            continue


def list_active_records() -> list[WorkerRecord]:
    """Scan ``workers/active/`` for every well-formed record. Malformed
    records are logged and skipped — recovery surfaces them separately
    via the scan_error attention bucket."""
    root = workers_active_dir()
    if not root.exists():
        return []
    out: list[WorkerRecord] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        path = child / "record.json"
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            out.append(WorkerRecord.model_validate(raw))
        except (OSError, ValueError) as exc:
            log.warning("worker record skipped (%s): %s", child.name, exc)
            continue
    return out


def archive_record(record: WorkerRecord) -> Path:
    """Move a terminal record from ``active/<id>/`` to
    ``archive/YYYY-MM/<id>/``. Idempotent: if the active dir is already
    gone, returns the archive path if present, else the would-be path."""
    if not record.is_terminal():
        raise ValueError(
            f"refuse to archive worker {record.id} in non-terminal status "
            f"{record.status.value}"
        )
    src = worker_dir(record.id)
    month = record.updated_at.astimezone(timezone.utc).strftime("%Y-%m")
    dst = workers_archive_dir() / month / record.id
    if dst.exists():
        return dst
    if not src.exists():
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(str(src), str(dst))
    # Phase 3 (2026-05-22): notify the operator the moment a worker leaves
    # the active queue. Mirrors the write_record broadcast pattern.
    from tesseract.orchestrator.workers.broadcast import fire_worker_broadcast

    fire_worker_broadcast("worker_record_archived", record)
    return dst


def _find_in_archive(worker_id: str) -> Path | None:
    """``mint_worker_id`` encodes ``YYYY-MM-DD`` at positions 3-13 of
    the id, so we can probe the right ``YYYY-MM`` bucket directly
    instead of walking every month. Fallback to a full scan if the id
    doesn't parse (legacy / hand-crafted ids in tests)."""
    root = workers_archive_dir()
    if not root.exists():
        return None
    parts = worker_id.split("-")
    if len(parts) >= 4 and parts[0] == "wk":
        candidate = root / f"{parts[1]}-{parts[2]}" / worker_id / "record.json"
        if candidate.exists():
            return candidate
    for month_dir in root.iterdir():
        if not month_dir.is_dir():
            continue
        candidate = month_dir / worker_id / "record.json"
        if candidate.exists():
            return candidate
    return None


__all__ = [
    "TERMINAL_STATUSES",
    "ArtifactRef",
    "Billing",
    "RiskClass",
    "RunOutcome",
    "StatusTransition",
    "WorkerRecord",
    "WorkerStatus",
    "archive_record",
    "iter_active_status_summary",
    "list_active_records",
    "load_record",
    "mint_worker_id",
    "write_record",
]
