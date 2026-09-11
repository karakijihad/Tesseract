"""Crash-storm circuit breaker.

Three ``crash`` intents within five minutes is a storm. A storm used to be the
end: the supervisor wrote ``<TESSERACT_HOME>/runtime/crash_storm.json``, exited
2, and refused every later start until a person cleared the marker. That is the
right answer to a machine destroying itself and the wrong one to a bad five
minutes, and the difference is knowable: **it waits and tries again, longer
each time, and latches only when the SAME failure survives every wait.**

A storm now returns a :class:`Storm` saying how long to wait and whether this
is the end. The supervisor pages on each one through the offline path, waits,
and respawns. The latch stays for exactly one case, and it is the *kernel
bugged* condition: the same crash signature (the exception's type and the
innermost frame it came from) after every backoff step. `clear_crash_storm` and
``--force`` are unchanged.

Stays separate from :mod:`tesseract.supervisor.daemon` so the
backoff/respawn loop reads cleaner and the breaker can be unit-tested
without spawning subprocesses.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from tesseract.supervisor.intent import runtime_dir

log = logging.getLogger(__name__)

CRASH_WINDOW_SECONDS = 300.0
CRASH_THRESHOLD = 3

#: How long the supervisor waits after each storm before trying again. Five
#: minutes, then fifteen, then an hour, and an hour for every storm after that.
#: The first is long enough for a transient to pass and short enough that a
#: person who is at the desk does not go and make coffee; the last is the
#: cadence of something nobody is watching.
STORM_BACKOFF_SECONDS: tuple[float, ...] = (300.0, 900.0, 3600.0)


def crash_storm_path(tesseract_home: Path) -> Path:
    return runtime_dir(tesseract_home) / "crash_storm.json"


def crash_storm_archive_dir(tesseract_home: Path) -> Path:
    """``runtime/logs/supervisor/crash-storm-archive/`` — every
    cleared marker lands here so the operator has a record of past
    storms across reboots."""
    base = tesseract_home.parent / "runtime" / "logs" / "supervisor" / "crash-storm-archive"
    base.mkdir(parents=True, exist_ok=True)
    return base


@dataclass
class CrashRecord:
    timestamp: datetime
    exit_code: int
    last_log_tail: str = ""
    #: What failed, as `signature_from_tail` reads it. Empty when the tail
    #: carried no traceback, which is not the same as a new failure and is
    #: why an empty one can never satisfy the latch.
    signature: str = ""

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "exit_code": self.exit_code,
            "last_log_tail": self.last_log_tail,
            "signature": self.signature,
        }


@dataclass(frozen=True)
class Storm:
    """What one crash storm asks of the supervisor.

    `latched` is the end: the marker is on disk and the supervisor exits. Every
    other storm is a wait and another try, which is what the operator asked
    for. `step` counts storms since this supervisor started, so it resets when
    a person restarts it and never persists a verdict across processes.
    """

    step: int
    wait_seconds: float
    latched: bool
    signature: str
    reason: str


#: The last frame of the last traceback in a console tail.
_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line \d+, in (?P<func>\S+)')
#: The line that ends a traceback: `module.Error: what happened`.
#: Anchored on the colon rather than on a list of exception suffixes,
#: because a class can be called anything and a list of the ones we
#: happened to think of would read a crash we have not seen as
#: unreadable.
_RAISED = re.compile(r"^(?P<exc>[A-Za-z_][\w.]*)(?::|$)")


def _how_long(seconds: float) -> str:
    """A wait in the words a person reads, and never "0 minutes"."""
    if seconds < 60:
        return f"{int(seconds)} seconds"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    hours = minutes / 60
    return f"{hours:.0f} hour{'' if hours == 1 else 's'}"


def signature_from_tail(tail: str) -> str:
    """What failed, in a form two crashes can be compared on.

    The exception's type and the innermost frame it came from, without the line
    number: a line number moves when anybody edits the file, and what this
    answers is whether the app is dying of the same thing, not whether the
    source is byte for byte what it was.

    `""` when the tail carries no traceback, and that is load bearing: an empty
    signature never equals another empty one for the purpose of latching, so a
    crash nobody can read keeps the supervisor trying rather than ending it on
    a guess.
    """
    if not tail:
        return ""
    lines = tail.splitlines()
    start = -1
    for idx, line in enumerate(lines):
        if line.strip().startswith("Traceback (most recent call last)"):
            start = idx
    if start < 0:
        return ""
    where = ""
    raised = ""
    for line in lines[start + 1:]:
        frame = _FRAME.match(line)
        if frame:
            where = f"{Path(frame.group('file')).name}:{frame.group('func')}"
            continue
        found = _RAISED.match(line.strip())
        if found and not line.startswith(" "):
            raised = found.group("exc")
    if not raised:
        return ""
    return f"{raised} at {where}" if where else raised


@dataclass
class CrashStormBreaker:
    """Rolling window over recent crash timestamps.

    Constructor takes paths so tests can isolate; production
    constructs it from ``TESSERACT_HOME``.
    """

    tesseract_home: Path
    threshold: int = CRASH_THRESHOLD
    window_seconds: float = CRASH_WINDOW_SECONDS
    backoff_seconds: tuple[float, ...] = STORM_BACKOFF_SECONDS
    _events: deque[CrashRecord] = field(default_factory=deque, init=False)
    # One entry per storm since this supervisor started, holding what failed.
    # In memory on purpose: the count is about this run of the process, and a
    # verdict that survived a restart would let a storm from Tuesday latch a
    # Wednesday that is failing for a different reason.
    _storms: list[str] = field(default_factory=list, init=False)

    # -- public surface ---------------------------------------------------

    def record_crash(
        self,
        *,
        exit_code: int,
        log_tail: str = "",
        signature: str = "",
        now: datetime | None = None,
    ) -> "Storm | None":
        """Append a crash, and say whether that makes a storm.

        `signature` is for a caller that already knows what failed and has no
        console tail to read it from. Left empty, it is derived from `log_tail`.

        `None` means the window is not full and the supervisor carries on with
        its ordinary short backoff. A :class:`Storm` means it waits the time
        the storm names, tells the operator, and tries again, unless
        `latched` is set, which is the end and is the caller's `sys.exit(2)`.
        The breaker stays passive so tests do not terminate the test runner.

        The window is CLEARED on a storm, so the next one needs another full
        set of crashes rather than arriving on the next single failure after a
        wait that already cost an hour.
        """
        when = now or datetime.now(timezone.utc)
        # A caller that KNOWS what failed says so; everything else is read out
        # of the console tail. The spawn-failure path has no tail to read,
        # because the process it would have come from never started, and
        # deriving nothing there meant the one failure mode with no way back
        # (the app cannot be started at all) was the one that could never
        # latch.
        signature = signature or signature_from_tail(log_tail)
        self._events.append(
            CrashRecord(
                timestamp=when,
                exit_code=exit_code,
                last_log_tail=log_tail,
                signature=signature,
            ),
        )
        self._evict_old(when)
        if len(self._events) < self.threshold:
            return None

        crashes = list(self._events)
        self._events.clear()
        self._storms.append(signature)
        step = len(self._storms)
        # The waits always climb, because how hard the machine is failing is
        # not a question about which bug it is. The LATCH is the one that needs
        # the same failure throughout: a run of different crashes is a bad
        # afternoon, and ending the app over it is the behaviour this replaced.
        wait = self.backoff_seconds[min(step, len(self.backoff_seconds)) - 1]
        # **The TRAILING ladder, never the whole history.** Asking whether every
        # storm this process has ever seen carried one signature meant a single
        # unrelated crash early in a long run disabled the latch for good: one
        # transient at breakfast, then a permanent bug all afternoon, and
        # `all(...)` stayed False for ever while the supervisor restart-looped
        # on an hour cadence. What the latch is about is the last full ladder,
        # so that is what it reads.
        ladder = self._storms[-(len(self.backoff_seconds) + 1):]
        same = bool(signature) and all(s == signature for s in ladder)
        latched = (
            same
            and len(ladder) > len(self.backoff_seconds)
            and step > len(self.backoff_seconds)
        )
        oldest = crashes[0].timestamp
        elapsed = (when - oldest).total_seconds()
        span = f"{int(elapsed // 60)}m{int(elapsed % 60)}s"
        if latched:
            reason = (
                f"{len(crashes)} crashes in {span}, and the same failure "
                f"({signature}) after every wait, so the app is not going to "
                f"come up by being started again"
            )
        else:
            reason = (
                f"{len(crashes)} crashes in {span}. Waiting {_how_long(wait)} "
                f"and starting it again"
            )
        if latched:
            self._latch(when, crashes=crashes, reason=reason, signature=signature)
        return Storm(
            step=step,
            wait_seconds=wait,
            latched=latched,
            signature=signature,
            reason=reason,
        )

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
        # The run of storms goes with the marker. A person clearing this has
        # said the cause is dealt with, so the next storm starts at the first
        # wait rather than at the end of a ladder they already answered.
        self._storms.clear()
        log.info("crash_storm: cleared marker → %s", target)
        return target

    # -- internal --------------------------------------------------------

    def _evict_old(self, now: datetime) -> None:
        cutoff = now.timestamp() - self.window_seconds
        while self._events and self._events[0].timestamp.timestamp() < cutoff:
            self._events.popleft()

    def _latch(
        self,
        now: datetime,
        *,
        crashes: list[CrashRecord],
        reason: str,
        signature: str,
    ) -> None:
        path = crash_storm_path(self.tesseract_home)
        payload = {
            "latched_at": now.isoformat(),
            "crashes": [c.to_dict() for c in crashes],
            # What survived every wait, which is the whole reason this latched
            # rather than backing off again. The operator reads this before
            # deciding whether clearing the marker is worth anything.
            "signature": signature,
            "storms": list(self._storms),
            "reason": reason,
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
    "STORM_BACKOFF_SECONDS",
    "Storm",
    "signature_from_tail",
    "crash_storm_path",
    "crash_storm_archive_dir",
]
