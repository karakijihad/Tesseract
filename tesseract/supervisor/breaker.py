"""Crash-storm circuit breaker.

Three ``crash`` intents within five minutes → the supervisor writes
``<TESSERACT_HOME>/runtime/crash_storm.json`` and exits 2. Next
``python -m tesseract.supervisor`` invocation refuses to start while
the marker exists; operator clears with ``python -m
tesseract.scripts.clear_crash_storm`` (archives the marker) or passes
``--force`` for one-shot bypass.

Stays separate from :mod:`tesseract.supervisor.daemon` so the
backoff/respawn loop reads cleaner and the breaker can be unit-tested
without spawning subprocesses.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from tesseract.supervisor.intent import runtime_dir

log = logging.getLogger(__name__)

CRASH_WINDOW_SECONDS = 300.0
CRASH_THRESHOLD = 3


def crash_storm_path(tesseract_home: Path) -> Path:
    return runtime_dir(tesseract_home) / "crash_storm.json"


def crash_storm_archive_dir(tesseract_home: Path) -> Path:
    """``<TESSERACT_HOME>/logs/supervisor/crash-storm-archive/`` — every
    cleared marker lands here so the operator has a record of past
    storms across reboots."""
    base = tesseract_home / "logs" / "supervisor" / "crash-storm-archive"
    base.mkdir(parents=True, exist_ok=True)
    return base


@dataclass
class CrashRecord:
    timestamp: datetime
    exit_code: int
    last_log_tail: str = ""

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "exit_code": self.exit_code,
            "last_log_tail": self.last_log_tail,
        }


@dataclass
class CrashStormBreaker:
    """Rolling window over recent crash timestamps.

    Constructor takes paths so tests can isolate; production
    constructs it from ``TESSERACT_HOME``.
    """

    tesseract_home: Path
    threshold: int = CRASH_THRESHOLD
    window_seconds: float = CRASH_WINDOW_SECONDS
    _events: deque[CrashRecord] = field(default_factory=deque, init=False)

    # -- public surface ---------------------------------------------------

    def record_crash(
        self,
        *,
        exit_code: int,
        log_tail: str = "",
        now: datetime | None = None,
    ) -> bool:
        """Append a crash. Returns True iff this push latched the
        storm. The caller is responsible for ``sys.exit(2)`` after a
        latch — the breaker stays passive so tests don't actually
        terminate the test runner."""
        when = now or datetime.now(timezone.utc)
        self._events.append(
            CrashRecord(timestamp=when, exit_code=exit_code, last_log_tail=log_tail),
        )
        self._evict_old(when)
        if len(self._events) >= self.threshold:
            self._latch(when)
            return True
        return False

    def is_latched(self) -> bool:
        return crash_storm_path(self.tesseract_home).exists()

    def clear(self) -> Path | None:
        """Archive the marker to ``crash-storm-archive/<timestamp>.json``
        and remove the live file. Returns the archive path (or None if
        no marker was latched). Operator-attended; called by both
        ``--force`` and ``clear_crash_storm.py``."""
        path = crash_storm_path(self.tesseract_home)
        if not path.exists():
            return None
        archive = crash_storm_archive_dir(self.tesseract_home)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        target = archive / f"{stamp}.json"
        # If two clears happen in the same second (operator runs --force
        # twice from script), suffix the path so we don't overwrite.
        i = 1
        while target.exists():
            target = archive / f"{stamp}-{i}.json"
            i += 1
        target.write_bytes(path.read_bytes())
        path.unlink()
        self._events.clear()
        log.info("crash_storm: cleared marker → %s", target)
        return target

    # -- internal --------------------------------------------------------

    def _evict_old(self, now: datetime) -> None:
        cutoff = now.timestamp() - self.window_seconds
        while self._events and self._events[0].timestamp.timestamp() < cutoff:
            self._events.popleft()

    def _latch(self, now: datetime) -> None:
        path = crash_storm_path(self.tesseract_home)
        oldest = self._events[0].timestamp
        elapsed = (now - oldest).total_seconds()
        payload = {
            "latched_at": now.isoformat(),
            "crashes": [c.to_dict() for c in self._events],
            "reason": (
                f"{len(self._events)} crashes in "
                f"{int(elapsed // 60)}m{int(elapsed % 60)}s — supervisor halted"
            ),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=".crash_storm-", suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
        except Exception:
            try:
                Path(tmp_name).unlink()
            except OSError:
                pass
            raise
        log.error("crash_storm: LATCHED — %s", payload["reason"])


__all__ = [
    "CrashStormBreaker",
    "CrashRecord",
    "CRASH_THRESHOLD",
    "CRASH_WINDOW_SECONDS",
    "crash_storm_path",
    "crash_storm_archive_dir",
]
