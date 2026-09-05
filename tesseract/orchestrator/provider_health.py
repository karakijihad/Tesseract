"""Provider-health JSONL telemetry.

Append-only per-ref log at
``runtime/logs/provider-health/<ref>.jsonl``.

**The unit of health is the ref, not the role.** A model either answers or it
does not; which roles happen to name it is not a property of the model. Keying
by role asked the same question once per naming role and reported one outage as
two defects — `cli.codex.gpt56_terra` was probed as `codex_cli` and again as
`auditor`, spending the subscription quota it was measuring to learn the same
fact twice. The roles that named the ref ride along in ``extra["roles"]``, so
the fan-out is still readable from the row.

Producers:

  * The :class:`tesseract.scheduler.tasks.provider_probe.ProviderProbeJob`
    (scheduled probe).
  * Production tripwires (``image_generate``'s uniform-image check,
    adapter HTTP error paths, paid-tool shape-mismatch detectors).

Consumers:

  * The ``provider_watch`` mapper — calls :func:`tail_recent` per ref
    every tick, drafts an agenda item when a 7-day window contains any
    ``ok=False`` row with a ``drift_kind`` it can route.
  * Operator (human) — JSONL is plain text; easy to ``cat`` or pipe.

Daily rotation gates on file size: a single ref's log over 10 MiB is
moved into ``logs/provider-health/archive/<ref>.<YYYYMMDD>.jsonl`` so
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

from tesseract.lib.log_envelope import BAD, INFO, WARN, envelope, parse_ts
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


def _ref_log_path(ref: str) -> Path:
    safe = ref.replace("/", "_").replace("\\", "_")
    return provider_health_dir() / f"{safe}.jsonl"


def _envelope_for(result: ProbeResult) -> dict:
    """This stream came closest to the envelope already and still needed one.

    It had `ok` and `drift_kind`, which is severity in two fields that a
    reader has to combine, and `ref`, which is a subject under another name.
    What it had no version of at all is a summary: every sentence about a
    provider in the report was composed by whoever read the row.
    """
    wearing = f" ({result.role})" if result.role else ""
    if result.ok:
        severity, said = INFO, "answered its check"
    elif result.drift_kind in {"latency_spike", "degraded"}:
        severity, said = WARN, f"is slow: {result.drift_kind}"
    else:
        severity, said = BAD, f"is failing: {result.drift_kind}"
    return envelope(
        stream="provider-health",
        severity=severity,
        subject=result.ref,
        summary=f"{result.ref}{wearing} {said}",
        outcome="ok" if result.ok else result.drift_kind,
        detail={"latency_ms": result.latency_ms, "probe_source": result.source},
        ts=parse_ts(result.probed_at) or datetime.now(timezone.utc),
    )


def record_probe_result(
    result: ProbeResult,
    *,
    publisher: "Callable[[ProbeResult], None] | None" = None,
) -> Path:
    """Append ``result`` as one JSONL row; rotate if oversized.

    Returns the path that was written. Both probes and production
    tripwires call this. ``publisher`` (optional) is the bus-publish
    hook; when supplied, drift rows (``ok=False``) also
    surface as ``provider_health`` events.

    Never raises — log-write failures are logged and dropped. A broken
    disk must not break the probe loop.
    """
    path = _ref_log_path(result.ref)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        row = {**asdict(result), **_envelope_for(result)}
        with path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(row, sort_keys=True))
            fh.write("\n")
    except Exception:  # noqa: BLE001
        log.exception("provider_health: failed to write row for ref=%s", result.ref)
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


def tail_recent(ref: str, n: int = 64) -> list[dict[str, Any]]:
    """Return the last ``n`` rows for ``ref`` newest-last.

    Cheap implementation — reads the file in one slurp and slices.
    The 10 MiB rotation cap keeps this bounded. Returns ``[]`` when
    the file does not exist (no probes have run yet)."""
    path = _ref_log_path(ref)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        log.exception("provider_health: tail_recent read failed (ref=%s)", ref)
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
    ref: str,
    *,
    days: int = 7,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return every row for ``ref`` whose ``probed_at`` is within the
    last ``days``. Used by the mapper to decide whether to draft a
    proposal — repeated drift in a rolling window is the signal."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    rows = tail_recent(ref, n=10_000)
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


def iter_refs_with_history() -> Iterable[str]:
    """Every ref that has at least one JSONL file on disk.

    A tree written before the keyspace moved still holds role-keyed files;
    they are telemetry, they age out of every window, and nothing here needs
    to tell the two apart."""
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

    **Single-writer contract, and where it now stops holding.** The probe
    and every in-backend tripwire serialise on one event loop, and the probe's
    two callers serialise on ``provider_probe._ONE_AT_A_TIME``. The lane
    tripwire does not: the agent controller is its own process, so two
    processes can observe the same oversize file and race on ``path.rename``
    — the loser drops a row on Windows or overwrites the archive on POSIX.
    The exposure is one row at a 10 MiB boundary, against a lane signal that
    is otherwise recorded nowhere; an OS-level file lock is what closes it if
    that trade stops being worth taking.
    """
    with suppress(OSError):
        if not path.exists() or path.stat().st_size < ROTATE_AT_BYTES:
            return
        archive = _archive_dir()
        archive.mkdir(parents=True, exist_ok=True)
        # ``time.strftime`` (rather than datetime) avoids importing yet
        # another timezone-aware moment per rotation; the suffix only
        # needs to be unique per day per ref.
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
    """Best-effort wrapper for production tripwires.

    Adapter ERROR-emit sites and paid tools (tavily_search /
    web_search / image_generate uniform-frame branch / etc.) call this
    when they detect a drift signal that the scheduled probe could
    have caught. The row is stamped ``source="production_tripwire"``
    so the ``provider_watch`` mapper can distinguish probe-time
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


#: What each fault kind looks like in the drift record the panel and the
#: watchman read. Two vocabularies on purpose: `drift_kind` is what was
#: OBSERVED (uniform output, an empty answer, a latency spike) and a fault
#: kind is WHY the provider refused, so neither can be derived from the other
#: in general. This table is the one place they meet.
#:
#: Total over `provider_failure.FaultKind` --- every kind that can be recorded,
#: not only the ones a sentence can be read as --- and `drift_kind_for_fault`
#: raises on a kind nobody placed. AR-17's rule: a default is how a new kind
#: goes quiet, and this is the table whose output becomes a headline.
_DRIFT_BY_FAULT: dict[str, str] = {
    # It answered and refused, or has not got what we asked for. Nothing about
    # the model is broken; it is not available to us until a person acts.
    "usage": "unavailable",
    "auth": "unavailable",
    "not_found": "unavailable",
    # It answered and rejected what we SENT. The fix is a parameter, which is
    # what `schema_error` says and what `http_error` hid: Brave named the
    # parameter it would not take and seven searches failed without it ever
    # reaching a screen.
    "schema": "schema_error",
    # It answered badly, or not for long enough. Worth trying again.
    "rate_limit": "http_error",
    "server": "http_error",
    "unknown": "http_error",
    # It answered, and there was nothing usable in the answer.
    "no_answer": "empty_output",
    "empty_answer": "empty_output",
    "shape_mismatch": "shape_mismatch",
    # We never got as far as asking, so nothing about the provider is known.
    # `unavailable` is the honest reading: not "it is broken", but "we could
    # not reach it".
    "check_failed": "unavailable",
}


def drift_kind_for_fault(kind: str) -> str:
    """The ``DriftKind`` a fault of this kind is recorded as.

    Raises on a kind nobody placed --- see `_DRIFT_BY_FAULT`.
    """
    return _DRIFT_BY_FAULT[kind]


def provider_drift_kind(error_text: str | None) -> str | None:
    """The ``DriftKind`` a free-text failure names, or ``None`` when the text
    is not about the provider at all.

    Used by callers that see every failure and only want the ones a probe
    could have caught — a lane turn fails for a hundred reasons the model
    itself is fine for, and recording those as provider drift would bury the
    one signal that matters. Deliberately narrow: a subscription that is out
    of quota or not signed in, and nothing else. Everything wider belongs in
    the caller's own error path, not in provider telemetry.
    """
    lowered = (error_text or "").lower()
    if not lowered:
        return None
    # No bare "quota": a delegate that fills a disk says "quota exceeded" too,
    # and a misfiled row is worse here than a missed one — this log is read as
    # the provider's condition.
    for needle in (
        "usage limit",
        "rate limit",
        "rate_limit",
        "unauthorized",
        "authentication",
        "not signed in",
        "please log in",
        "please login",
    ):
        if needle in lowered:
            return "unavailable"
    return None


__all__ = [
    "ROTATE_AT_BYTES",
    "Publisher",
    "drift_kind_for_fault",
    "iter_refs_with_history",
    "note_production_tripwire",
    "provider_drift_kind",
    "provider_health_dir",
    "record_probe_result",
    "rolling_window",
    "tail_recent",
]
