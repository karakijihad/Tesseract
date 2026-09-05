"""Who is running the pipeline right now, and how anybody else finds out.

Four callers can start pipeline work and until now none of them could see the
others: the scheduler firing a row, `--row` and `--stage` from a terminal, and
`pipeline_run_stage` from the panel, a channel or the model. The tool declares
`is_concurrency_safe() -> False`, which serialises tool dispatch inside ONE
turn and says nothing about the other three. Each caller built its own
`PipelineRunner` and ran.

Two things that costs, and the second is worse than the first:

* Two stages executing at once against one artifact store, one watermark store
  and one manifest slot, which is the ordering the whole pipeline exists to
  enforce.
* `--row` while the backend's own row is open. `PipelineRunner.run` resumes an
  unfinished run, so the terminal ADOPTS the manifest the backend is writing,
  appends its stage rows into it, and both processes commit over each other.
  Whichever reaches `finish` first clears `current.json`; the other overwrites
  the closed record. Not two runs racing: two processes writing one record.

**The lock is an OS advisory lock, and that is the whole reason it is safe.**
The kernel releases it when the holder dies, so a killed backend cannot leave
the pipeline unrunnable and there is no expiry constant, no heartbeat and no
pid-liveness guess to get wrong. `workspace_events/events.py::_interprocess_lock`
is the same primitive; this one asks for it without blocking, because the
answer to contention here is a sentence rather than a wait.

**It is NOT `current.json`, and the difference is easy to lose.** That file
says a run was interrupted and can be resumed, so it MUST survive a crash. A
lock says a process is running right now, so it must NOT. They look like one
question and they are two, and reusing one for the other would either wedge
the pipeline after a kill or hand a resume point to a second process.

**One lock for the whole pipeline root, not one per stage.** Stages are not
independent: they share the artifact store, the watermarks and the manifest,
which is exactly why a graph orders them. Locking per stage would permit the
collision the ordering exists to prevent.

**If the platform primitive is missing, it falls open and says so.** Failing
closed would mean the nightly pass never runs, and falling open is precisely
today's behaviour, so it cannot be a regression. The warning names the
consequence rather than the mechanism.

**It is not the scheduler's seat, and the difference is worth stating once.**
`engine.py::_claim` already refuses a second hand-fired run of the same JOB,
and it is the right mechanism for what it covers: the doors the engine owns,
in the process the engine runs in, keyed on a job name. It cannot see a bare
CLI process, because the seat is a set in memory; and it cannot see a STAGE at
all, because `pipeline_run_stage` reaches `run_one` without passing through
the engine. So the seat answers "is this job already running here" and this
answers "is anything running the pipeline anywhere on this machine". Two
questions, two scopes, and the seat is the cheaper one where it applies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, AsyncIterator

log = logging.getLogger(__name__)

#: What a hand-fired run waits. Nothing, and it is a property rather than a
#: tuned number: the caller is a chat turn, a button press or somebody at a
#: terminal, and a nightly row takes minutes. Being told is the answer; being
#: made to wait is not.
NEVER_WAITS = 0.0

#: What a scheduled row waits for a stage somebody fired by hand. The other
#: half of the asymmetry: a hand-fired stage takes seconds and its caller is
#: standing there to be told, so it never waits; a night that quietly did not
#: happen costs more than a minute of patience, so the row does. Bounded rather
#: than indefinite, so a wedged stage cannot stall the scheduler with nothing
#: saying why.
#:
#: A constant and not a config row, for the reason `_RESUME_MAX_AGE` and
#: `_DUE_SLACK` are constants in `runner.py`: this is the pipeline's own
#: timing rather than a number an operator reaches for. Contention here is
#: rare, and what a person needs when it happens is the sentence, not a dial.
A_ROW_WAITS = 60.0

#: How often a waiting row asks again. Small enough that the row starts
#: promptly when a short hand-fired stage lets go, large enough that waiting
#: is not a spin. Waited on with `asyncio.sleep`, never `time.sleep`: the row
#: is awaited on the loop that carries health, the socket and inbound turns,
#: and a minute of blocking there would be a worse outage than the collision
#: this prevents. Every other operation here is one filesystem call and stays
#: inline.
_RETRY_EVERY = 0.25


def lock_path() -> Path:
    from tesseract.scheduler.pipeline.artifacts import pipeline_root

    return pipeline_root() / ".lock"


def holder_path() -> Path:
    """Who holds it, beside the lock rather than inside it.

    Separate because the lock is a byte range the OS makes unreadable to
    anybody else while it is held, and the whole point of recording a holder
    is that the process turned away can read it. A holder file left behind by
    a crash is never read: it is only consulted when acquisition FAILED, and
    acquisition cannot fail once the kernel has released a dead holder's lock.
    """
    from tesseract.scheduler.pipeline.artifacts import pipeline_root

    return pipeline_root() / ".holder.json"


@dataclass(frozen=True)
class Holder:
    """The run that has it, in the words a refusal is written from."""

    what: str
    run_id: str
    pid: int
    since: datetime

    def in_words(self, now: datetime | None = None) -> str:
        at = now or datetime.now(timezone.utc)
        seconds = max(0.0, (at - self.since).total_seconds())
        held = f"{seconds:.0f}s" if seconds < 90 else f"{seconds / 60:.0f} minutes"
        return f"{self.what} has been running for {held}"


@dataclass(frozen=True)
class Taken:
    """Whether we got it, and who has it if we did not."""

    ok: bool
    holder: Holder | None = None

    def why_not(self, *, wanted: str) -> str:
        """One sentence for whoever asked, naming what to do about it.

        Composed here so the row, the terminal and the tool all say the same
        thing: a caller writing its own would be a second author of one fact.
        """
        who = self.holder.in_words() if self.holder else "something else is running it"
        return (
            f"{wanted} was not started: {who}, and the pipeline runs one thing "
            "at a time so they do not write over each other. Ask again when it "
            "has finished"
        )


def _read_holder() -> Holder | None:
    try:
        raw = json.loads(holder_path().read_text(encoding="utf-8"))
        return Holder(
            what=str(raw["what"]),
            run_id=str(raw["run_id"]),
            pid=int(raw["pid"]),
            since=datetime.fromisoformat(str(raw["since"])),
        )
    except (OSError, ValueError, KeyError, TypeError):
        # A refusal that cannot name the holder is still a refusal, and it is
        # a better answer than an exception thrown while explaining one.
        return None


def _write_holder(holder: Holder) -> None:
    try:
        holder_path().write_text(
            json.dumps(
                {
                    "what": holder.what,
                    "run_id": holder.run_id,
                    "pid": holder.pid,
                    "since": holder.since.isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        # Best effort. Holding the lock is what matters; being able to say who
        # holds it is what makes the refusal readable, and losing the second
        # is not a reason to give up the first.
        log.warning("pipeline lock: could not record who holds it", exc_info=True)


def _try_lock(fh: IO[bytes]) -> bool | None:
    """True if taken, False if somebody has it, None if there is no primitive."""
    if sys.platform == "win32":
        try:
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except ImportError:
            return None
        except OSError:
            return False
    try:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except ImportError:
        return None
    except OSError:
        return False


def _unlock(fh: IO[bytes]) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except (OSError, ImportError):
        log.exception("pipeline lock: release failed")


@asynccontextmanager
async def hold(
    what: str, run_id: str, *, wait_seconds: float = NEVER_WAITS
) -> AsyncIterator[Taken]:
    """Hold the pipeline for the length of the block, or say who has it.

    `what` is the row or stage being asked for, and it is what the next caller
    turned away will be told is running.

    Async because of the wait and only because of it. The lock itself belongs
    to the open file, not to a thread, so it is taken and released inline.
    """
    path = lock_path()
    fh: IO[bytes] | None = None
    locked = False
    try:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # `a+b` creates without truncating, so two processes arriving
            # together are locking one file rather than each making their own.
            fh = open(path, "a+b")
        except OSError:
            log.warning(
                "pipeline lock: %s could not be opened, so this run is not "
                "protected from another one starting beside it",
                path, exc_info=True,
            )
            yield Taken(ok=True)
            return

        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            got = _try_lock(fh)
            if got is None:
                log.warning(
                    "pipeline lock: this platform has no lock primitive, so "
                    "%s is running unprotected and a second run could start "
                    "beside it", what,
                )
                yield Taken(ok=True)
                return
            if got:
                locked = True
                break
            if time.monotonic() >= deadline:
                yield Taken(ok=False, holder=_read_holder())
                return
            await asyncio.sleep(_RETRY_EVERY)

        _write_holder(
            Holder(
                what=what,
                run_id=run_id,
                pid=os.getpid(),
                since=datetime.now(timezone.utc),
            )
        )
        yield Taken(ok=True)
    finally:
        if locked and fh is not None:
            try:
                holder_path().unlink(missing_ok=True)
            except OSError:
                pass
            _unlock(fh)
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass


__all__ = ["A_ROW_WAITS", "Holder", "NEVER_WAITS", "Taken", "hold", "holder_path", "lock_path"]
