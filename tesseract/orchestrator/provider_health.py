"""Provider-health JSONL telemetry — AU-14 substrate.

Append-only per-role log at
``<TESSERACT_HOME>/logs/provider-health/<role>.jsonl``. Producers:

  * The :class:`tesseract.scheduler.tasks.provider_probe.ProviderProbeJob`
    (scheduled probe).
  * Future production tripwires from AU-14 Session 14b
    (``image_generate``'s uniform-image check, adapter HTTP error paths,
    paid-tool shape-mismatch detectors).

Consumers:

  * AU-5's ``provider_watch`` mapper — calls :func:`tail_recent` per role
    every tick, drafts an agenda item when a 7-day window contains any
    ``ok=False`` row with a ``drift_kind`` it can route.
  * Operator (human) — JSONL is plain text; easy to ``cat`` or pipe.

Daily rotation gates on file size: a single role's log over 10 MiB is
moved into ``logs/provider-health/archive/<role>.<YYYYMMDD>.jsonl`` so
``tail_recent`` doesn't have to walk an unbounded file. Archives stay
forever — operator can prune by hand.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import suppress
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from tesseract.scheduler.tasks._probes.base import ProbeResult

log = logging.getLogger(__name__)

# Rotation trigger — at write time, if the file is over this size we
# move it to archive/ and start fresh. 10 MiB ≈ ~60k probe rows for
# the current probe shape — far more than a 7-day window needs.
ROTATE_AT_BYTES = 10 * 1024 * 1024


def _resolve_home() -> Path:
    """Resolve ``TESSERACT_HOME`` at call time so tests that set
    ``monkeypatch.setenv("TESSERACT_HOME", tmp_path)`` see the override
    even when this module was imported earlier in the session.

    The ``tesseract.paths`` import is deferred to call time so a fixture
    that monkeypatches the env var BEFORE first call hits the env branch
    every time — the structural guarantee, not just the empirical one.
    """
    env = os.environ.get("TESSERACT_HOME")
    if env:
        return Path(env).resolve()
    from tesseract.paths import TESSERACT_DIR
    return TESSERACT_DIR


def provider_health_dir() -> Path:
    from tesseract.paths import log_dir

    return log_dir("provider-health")


def _archive_dir() -> Path:
    return provider_health_dir() / "archive"


def _role_log_path(role: str) -> Path:
    safe = role.replace("/", "_").replace("\\", "_")
    return provider_health_dir() / f"{safe}.jsonl"


def record_probe_result(
    result: ProbeResult,
    *,
    publisher: "Callable[[ProbeResult], None] | None" = None,
) -> Path:
    """Append ``result`` as one JSONL row; rotate if oversized.

    Returns the path that was written. Both probes and production
    tripwires call this. ``publisher`` (optional) is the AU-4 / AU-5
    bus-publish hook; when supplied, drift rows (``ok=False``) also
    surface as ``provider_health`` events.

    Never raises — log-write failures are logged and dropped. A broken
    disk must not break the probe loop.
    """
    path = _role_log_path(result.role)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        row = asdict(result)
        with path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(row, sort_keys=True))
            fh.write("\n")
    except Exception:  # noqa: BLE001
        log.exception("provider_health: failed to write row for role=%s", result.role)
        return path

    if not result.ok and publisher is not None:
        try:
            publisher(result)
        except Exception:  # noqa: BLE001
            log.exception(
                "provider_health: publisher raised for role=%s drift=%s",
                result.role,
                result.drift_kind,
            )
    return path


# ── Read API ─────────────────────────────────────────────────


def tail_recent(role: str, n: int = 64) -> list[dict[str, Any]]:
    """Return the last ``n`` rows for ``role`` newest-last.

    Cheap implementation — reads the file in one slurp and slices.
    The 10 MiB rotation cap keeps this bounded. Returns ``[]`` when
    the file does not exist (no probes have run yet)."""
    path = _role_log_path(role)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        log.exception("provider_health: tail_recent read failed (role=%s)", role)
        return []
    out: list[dict[str, Any]] = []
    for raw in lines[-n:]:
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            out.append(json.loads(stripped))
        except json.JSONDecodeError:
            continue
    return out


def rolling_window(
    role: str,
    *,
    days: int = 7,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return every row for ``role`` whose ``probed_at`` is within the
    last ``days``. Used by AU-5's mapper to decide whether to draft a
    proposal — repeated drift in a rolling window is the signal."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    rows = tail_recent(role, n=10_000)
    out: list[dict[str, Any]] = []
    for row in rows:
        probed_at = row.get("probed_at")
        if not isinstance(probed_at, str):
            continue
        try:
            ts = datetime.fromisoformat(probed_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            out.append(row)
    return out


def iter_roles_with_history() -> Iterable[str]:
    """Every role that has at least one JSONL file on disk."""
    base = provider_health_dir()
    if not base.exists():
        return ()
    return tuple(p.stem for p in base.glob("*.jsonl") if p.is_file())


# ── Internals ────────────────────────────────────────────────


def _rotate_if_needed(path: Path) -> None:
    """Move ``path`` to ``archive/<stem>.<YYYYMMDD>.jsonl`` when it
    exceeds ``ROTATE_AT_BYTES``. Best-effort: a rename failure leaves
    the file in place — the next write just appends to the oversize
    file. Not a correctness issue (only a tail-cost issue).

    **Single-writer contract.** AU-14 Session 14a's only producer is
    ``ProviderProbeJob`` (one asyncio task per scheduler tick — no
    intra-job concurrency). Session 14b adds production tripwires that
    will share this writer; when those land they MUST either serialise
    through a single queue or hold an OS-level file lock, because two
    callers can otherwise both observe an oversize file and race on
    ``path.rename`` — the loser silently appends to a fresh post-
    rotation file and drops a probe row on Windows or overwrites the
    archive on POSIX.
    """
    with suppress(OSError):
        if not path.exists() or path.stat().st_size < ROTATE_AT_BYTES:
            return
        archive = _archive_dir()
        archive.mkdir(parents=True, exist_ok=True)
        # ``time.strftime`` (rather than datetime) avoids importing yet
        # another timezone-aware moment per rotation; the suffix only
        # needs to be unique per day per role.
        stamp = time.strftime("%Y%m%d", time.gmtime())
        target = archive / f"{path.stem}.{stamp}.jsonl"
        # If the operator forces multiple rotations in one UTC day, append a
        # nanosecond suffix so we don't overwrite.
        if target.exists():
            target = archive / f"{path.stem}.{stamp}.{time.time_ns()}.jsonl"
        path.rename(target)


# Public alias for the publisher callable shape so callers (probe jobs,
# production tripwires) can type their kwarg without importing the bus.
Publisher = Callable[[ProbeResult], None]


def note_production_tripwire(
    role: str,
    ref: str,
    drift_kind: str,
    evidence: dict[str, Any] | None = None,
    *,
    publisher: "Callable[[ProbeResult], None] | None" = None,
    latency_ms: float = 0.0,
) -> Path | None:
    """Best-effort wrapper for AU-14 14b production tripwires.

    Adapter ERROR-emit sites and paid tools (tavily_search /
    web_search / image_generate uniform-frame branch / etc.) call this
    when they detect a drift signal that the scheduled probe could
    have caught. The row is stamped ``source="production_tripwire"``
    so the AU-5 ``provider_watch`` mapper can distinguish probe-time
    drift from real-traffic drift.

    Empty / unknown ``role`` or ``ref`` skips the write — anonymous
    rows would pollute the JSONL keyspace.

    Never raises: failures are logged and dropped so a broken
    telemetry surface cannot break the call path.
    """
    if not role or not ref:
        return None
    try:
        result = ProbeResult(
            role=role,
            ref=ref,
            ok=False,
            drift_kind=drift_kind,  # type: ignore[arg-type]
            evidence=dict(evidence or {}),
            probed_at=datetime.now(timezone.utc).isoformat(),
            latency_ms=latency_ms,
            source="production_tripwire",
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "provider_health: ProbeResult construction failed (role=%s drift=%s)",
            role,
            drift_kind,
        )
        return None
    return record_probe_result(result, publisher=publisher)


__all__ = [
    "ROTATE_AT_BYTES",
    "Publisher",
    "iter_roles_with_history",
    "note_production_tripwire",
    "provider_health_dir",
    "record_probe_result",
    "rolling_window",
    "tail_recent",
]
