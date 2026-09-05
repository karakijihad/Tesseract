"""Generic circuit breaker.

MAX_FAILURES consecutive failures → the breaker opens and the subsystem behind
it is skipped. Opens are logged to logs/circuit-breakers/{name}.jsonl. The
session continues without the broken subsystem (degradation, not failure).

An open breaker is not a permanent one. Once the cooldown has elapsed the next
call is let through as a probe: it succeeds and the breaker closes, or it fails
and the breaker opens again, for longer each time. That is what makes recovery
something the runtime does rather than something a person has to come back and
do — a breaker that latched on a spending cap once kept every proactive wake in
the runtime switched off for 26 hours after the cap had been lifted.

Eight properties, and they hold AT ONCE. This mechanism has twice been edited
against whichever one was in front of the author, and both times the edit
dropped one of the others. Check a change against the whole list.

  1. A failing mechanism is not retried in a hot loop. The original purpose.
     Nothing below may cost this.
  2. It can close again without a human.
  3. Its state survives a restart without becoming permanent. 2 and 3 are the
     pair that was never held together: persistence is right, permanence is
     the bug.
  4. It never counts a failure it did not cause.
  5. It can say what it refused, and what that cost.
  6. The operator can see and clear it from any surface.
  7. Failing closed stays possible where that is correct. A crash storm must
     stop, and `supervisor/breaker.py` is that case.
  8. It never blocks the only path that could heal it.

Two questions, and they are not the same question:

  `is_open`    — the circuit is open. Nothing has succeeded since it tripped and
                 no reset has been written. This is what the JSONL says, what
                 the cockpit shows and what the watchman reports.
  `is_tripped` — it is refusing right now. False during a probe window, while
                 `is_open` is still true.

Gates ask `allow(subject)`, which is `is_tripped` plus a record of what was
turned away; anything reporting state reads `is_open` or the file. Both sides
derive open-ness from the same events, so a file reader and a live object cannot
disagree about whether a breaker is open.

A gate that just returns leaves nobody able to say whether the outage cost
anything. `allow` takes the name of the work being refused and keeps one entry
per name for as long as the outage lasts, so the question "did this keep
something from someone" has an answer here rather than an inference drawn from
somewhere else. The caller names its subject; whether that subject was worth
telling the operator about is one policy, in one place, and not this one.

`is_tripped` is a plain read with no side effect, so a status check never eats
the probe a gate was about to take. The cost is that two callers arriving in the
same instant can both pass; the first failure re-opens the breaker behind them,
so the exposure is one extra call and not a loop (invariant 1).

Cooldown ≤ 0 disables the probe and the breaker stays open until something
resets it, by the same convention `adapter_chain._EntryBreaker` already uses.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from functools import lru_cache, partial
from pathlib import Path
from typing import Callable, Protocol
from weakref import WeakSet

import yaml

from tesseract.paths import CONFIG_DIR

logger = logging.getLogger(__name__)

#: How much of a failure's text is kept. The same cap on the file and on the
#: bus: `breaker_status` is an `auto` tool, so whatever is written here reaches
#: the model's own context without anyone being asked, and an exception string
#: is the least predictable thing in this module.
ERROR_CAP = 200


def _read_availability() -> dict:
    cfg_path = CONFIG_DIR / "providers.yaml"
    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg["availability"]


@lru_cache(maxsize=1)
def _default_max_failures() -> int:
    return int(_read_availability()["max_consecutive_failures"])


@lru_cache(maxsize=1)
def _cooldown_policy() -> tuple[float, float, float]:
    """(first cooldown, multiplier per repeat trip, ceiling) in seconds."""
    availability = _read_availability()
    return (
        float(availability["cooldown_seconds"]),
        float(availability["cooldown_backoff"]),
        float(availability["cooldown_max_seconds"]),
    )


@lru_cache(maxsize=1)
def _denial_ledger_size() -> int:
    return int(_read_availability()["denial_ledger_size"])


@dataclass(frozen=True)
class Denial:
    """One thing a breaker turned away, and how often it has since.

    `count` is what this process has seen. A restart carries the subject and
    `first_at` across, because what was refused and since when is the part the
    operator needs; it does not carry the tally, which would otherwise have to
    be rewritten on every repeat.
    """

    subject: str
    first_at: datetime
    last_at: datetime
    count: int


def retry_phrase(seconds: float | None) -> str:
    """The three answers `remaining_cooldown` gives, in words.

    Here rather than beside either reader, because `breaker_status` and the
    recovery pass both say this sentence and two versions of it would drift the
    way every other pair in this file already has.
    """
    if seconds is None:
        return "not going to try again by itself"
    if seconds <= 0:
        return "trying again on the next call"
    minutes = int(seconds // 60)
    if minutes < 1:
        return f"trying again in {int(seconds)} seconds"
    return f"trying again in {minutes} minute{'' if minutes == 1 else 's'}"


def _cooldown_span(base: float, backoff: float, ceiling: float, trips: int) -> float:
    """How long the `trips`-th consecutive trip refuses for.

    A module function because the file-derived report has to answer it too, for
    a breaker with no live object, and answering it a second way is how the two
    halves of the report end up disagreeing.
    """
    if base <= 0:
        return 0.0
    return min(base * (backoff ** max(0, trips - 1)), ceiling)


def _trips_since_reset(events: list[dict]) -> int:
    """Consecutive trips with no reset between them. What the backoff counts."""
    trips = 0
    for event in events:
        kind = event.get("event")
        if kind == "tripped":
            trips += 1
        elif kind == "reset":
            trips = 0
    return trips


def _read_time(raw: object) -> datetime | None:
    """A logged timestamp as an aware datetime. Naive means local, which is
    what `_log_trip` writes."""
    if not isinstance(raw, str):
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.astimezone()


def read_denials(path: Path) -> dict[str, Denial]:
    """The persisted ledger at `path`, oldest first.

    A module function rather than a method because the report has to answer
    "what did it turn away" for a breaker whose subsystem has not been built
    in this process, and there is no object to ask. `count` starts at 1: the
    file carries what was refused and since when, never the tally.
    """
    out: dict[str, Denial] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        subject = row.get("subject")
        when = _read_time(row.get("first_at"))
        if not isinstance(subject, str) or when is None:
            continue
        out[subject] = Denial(subject=subject, first_at=when, last_at=when, count=1)
    return out


def denials_path(log_dir: Path, name: str) -> Path:
    """Where a breaker's ledger lives. A subdirectory, because every reader of
    the breaker log globs `*.jsonl` beside it and would otherwise read this as
    a breaker of its own."""
    return log_dir / "denials" / f"{name}.jsonl"


#: Every breaker alive in this process, by name, held weakly so a subsystem
#: that goes away takes its breaker with it. This is what lets the operator
#: clear one from anywhere: the file says what is open, but only the live
#: object can be closed, and a gate reads the object.
#:
#: A SET per name, not one object: `vault_lint` builds a fresh breaker on every
#: run, so two can be alive at once, and closing only the newest would report
#: success while the run actually holding a gate went on refusing.
_LIVE: "dict[str, WeakSet[CircuitBreaker]]" = {}


def live_breakers(name: str) -> list["CircuitBreaker"]:
    """Every breaker of that name alive in this process."""
    return list(_LIVE.get(name, ()))


class Closeable(Protocol):
    """What a breaker of another class has to be able to answer.

    Three questions and no more: is it refusing right now, how long until it
    stops, and what is it refusing for. A `CircuitBreaker` answers them out of
    its own fields; anything else answers them however it likes.
    """

    def is_open_now(self) -> bool: ...
    def remaining_now(self) -> float: ...
    def says(self) -> str: ...
    def close(self) -> None: ...


#: Breakers that are not `CircuitBreaker` and hold the same kind of door shut.
#: The chain's per-entry cooldowns are the first: an operator who has just
#: added credits is asking one question, `stop waiting and try it now`, and it
#: does not become two questions because the runtime keeps the two waits in
#: two classes.
#:
#: **This registry is an inversion, and that is the whole point.** This module
#: imports `tesseract.paths` and nothing else, which is what lets everything
#: from `kernel` to `mirror` depend on it. Reaching up into `brain` to find the
#: chain's breakers would invert that for one report. So the chain hands its
#: breakers in and this module never reaches for them.
#:
#: Weak, and a SET per name, for the reasons `_LIVE` is: a session that ends
#: takes its breakers with it, and several can be alive for one name at once
#: (a chain entry has one breaker per `FallbackAdapter`, and `fork()` makes
#: more), so closing the newest and reporting success would leave the others
#: refusing.
_FOREIGN: "dict[str, WeakSet[Closeable]]" = {}


def register_foreign_breaker(name: str, breaker: "Closeable") -> None:
    """Add a breaker of another class to what `breaker_status` reports and
    `breaker_reset` can clear."""
    _FOREIGN.setdefault(name, WeakSet()).add(breaker)


def foreign_breakers(name: str) -> "list[Closeable]":
    return list(_FOREIGN.get(name, ()))


def _foreign_names() -> list[str]:
    """Names with at least one live foreign breaker behind them.

    Read through the WeakSet rather than off the dict keys: a name whose
    breakers have all been collected is a session that ended, and reporting it
    would offer the operator a control with nothing behind it.
    """
    return sorted(name for name, held in _FOREIGN.items() if len(held) > 0)


def _representative(name: str) -> "CircuitBreaker | None":
    """The one worth reporting on: an open one if any is, else any at all."""
    live = live_breakers(name)
    return next((b for b in live if b.is_open), live[0] if live else None)


def _merged_denials(name: str) -> tuple[Denial, ...]:
    """What every live breaker of this name has turned away.

    `clear_breaker` already closes all of them, so reporting on one and
    ignoring the rest would say less than the same code acts on. Two runs
    refusing different work is the case, and it is `vault_lint`'s.
    """
    merged: dict[str, Denial] = {}
    for breaker in live_breakers(name):
        for denial in breaker.denials():
            held = merged.get(denial.subject)
            if held is None:
                merged[denial.subject] = denial
                continue
            merged[denial.subject] = replace(
                held,
                first_at=min(held.first_at, denial.first_at),
                last_at=max(held.last_at, denial.last_at),
                count=held.count + denial.count,
            )
    return tuple(merged.values())


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        max_failures: int | None = None,
        log_dir: Path | None = None,
        cooldown_seconds: float | None = None,
        denial_ledger_size: int | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        if max_failures is None:
            max_failures = _default_max_failures()
        base, backoff, ceiling = _cooldown_policy()
        self.name = name
        self.max_failures = max_failures
        self.cooldown_seconds = base if cooldown_seconds is None else float(cooldown_seconds)
        self._backoff = backoff
        self._cooldown_ceiling = ceiling
        self._now = now_fn if now_fn is not None else (lambda: datetime.now().astimezone())
        self.failure_count = 0
        self.is_open = False
        #: When the next probe is allowed. `None` while closed, and while open
        #: with the cooldown disabled — which is the one case that stays open
        #: until something resets it.
        self._open_until: datetime | None = None
        #: Trips since the last reset. Drives the backoff.
        self._trips = 0
        #: What stopped it, and when. Held on the object rather than read back
        #: out of the file each time, because a breaker with no `log_dir` has
        #: no file and still has to be able to say what it refused and why.
        #: Both are cleared on close: they describe THIS outage.
        self._last_error = ""
        self._tripped_at: datetime | None = None
        #: When the breaker last went from closed to open. Distinct from
        #: `_tripped_at`, which moves forward on every failed probe: this is
        #: what identifies the OUTAGE, and it has to stand still for as long as
        #: the breaker stays open.
        self._opened_at: datetime | None = None
        self._log_dir = log_dir
        #: What this outage has turned away, oldest first, capped. Cleared when
        #: the breaker closes, because it answers what THIS outage cost.
        self._denials: dict[str, Denial] = {}
        self._denial_cap = (
            _denial_ledger_size() if denial_ledger_size is None else int(denial_ledger_size)
        )
        self._rehydrate_from_log()
        _LIVE.setdefault(self.name, WeakSet()).add(self)

    @property
    def is_tripped(self) -> bool:
        """Is this breaker refusing calls right now?"""
        if not self.is_open:
            return False
        if self._open_until is None:
            return True
        return self._now() < self._open_until

    def allow(self, subject: str) -> bool:
        """May this call go through? Records what was turned away if it may not.

        The one way a gate asks. `subject` names the work being refused, in
        enough detail to tell two of them apart: the ledger holds one entry per
        subject, so a sweep that asks every minute for an hour leaves one line
        and not sixty. What a subject is worth telling the operator about is
        one policy in one place, and it is not this one.
        """
        if not self.is_tripped:
            return True
        self._record_denial(subject)
        return False

    def denials(self) -> tuple[Denial, ...]:
        """What this outage has turned away, oldest first."""
        return tuple(self._denials.values())

    def _record_denial(self, subject: str) -> None:
        if self._denial_cap <= 0:
            return
        now = self._now()
        known = self._denials.get(subject)
        if known is not None:
            # The subject set has not changed, so neither has the file. This is
            # what keeps a gate inside a loop off the disk.
            self._denials[subject] = replace(known, last_at=now, count=known.count + 1)
            return
        self._denials[subject] = Denial(subject=subject, first_at=now, last_at=now, count=1)
        while len(self._denials) > self._denial_cap:
            self._denials.pop(next(iter(self._denials)))
        self._write_denials()
        self._escalate(subject)

    def _denials_path(self) -> Path | None:
        if self._log_dir is None:
            return None
        return denials_path(self._log_dir, self.name)

    def _write_denials(self) -> None:
        path = self._denials_path()
        if path is None:
            return
        lines = [
            json.dumps({"subject": d.subject, "first_at": d.first_at.isoformat()})
            for d in self._denials.values()
        ]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        except OSError:
            logger.exception("could not record what breaker '%s' refused", self.name)

    def _load_denials(self) -> None:
        path = self._denials_path()
        if path is None or self._denial_cap <= 0:
            return
        self._denials.update(read_denials(path))
        while len(self._denials) > self._denial_cap:
            self._denials.pop(next(iter(self._denials)))

    def _clear_denials(self) -> None:
        self._denials.clear()
        path = self._denials_path()
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.exception("could not clear what breaker '%s' refused", self.name)

    def remaining_now(self) -> float:
        """`remaining_cooldown` as the `Closeable` protocol wants it: a number.

        The protocol's readers are reporting surfaces that want one figure to
        show. `None` (no probe is coming) becomes 0 there, and the sentence
        `says` builds is what carries that distinction to a person.
        """
        return self.remaining_cooldown() or 0.0

    def says(self) -> str:
        """What this breaker is doing, for a person to read.

        `_EntryBreaker` had this and the class the protocol was written around
        did not, so every caller reaching for it got an `AttributeError` — and
        a caller that swallows (a gate must) turned that into silence. The
        sentence names the cause and the way out, because a reader who cannot
        tell what will now go wrong has not been told anything.
        """
        if not self.is_open:
            return "is answering."
        cause = f" ({self._last_error})" if self._last_error else ""
        remaining = self.remaining_cooldown()
        if remaining is None:
            return f"stopped answering{cause} and will not be tried again until it is reset."
        if remaining <= 0:
            return f"stopped answering{cause} and is about to be tried again."
        return (
            f"stopped answering{cause} and tries again in "
            f"{int(remaining // 60)}m{int(remaining % 60):02d}s."
        )

    def remaining_cooldown(self) -> float | None:
        """Seconds until the next probe is allowed. 0 when one is allowed now,
        and None when there will not be one.

        The three answers are different and were two: an open breaker with the
        probe disabled reported 0, which reads as "trying again on the next
        call" and is the opposite of what it does. The file-derived answer had
        it right, so the two sides of the report disagreed about one state.
        """
        if not self.is_open:
            return 0.0
        if self._open_until is None:
            return None
        return max(0.0, (self._open_until - self._now()).total_seconds())

    def _cooldown_for(self, trips: int) -> float:
        return _cooldown_span(
            self.cooldown_seconds, self._backoff, self._cooldown_ceiling, trips
        )

    def _escalate(self, subject: str) -> None:
        """Hand one denial to the recovery pass, which decides if it matters.

        Guarded and late-imported for the reason `_publish` is: a gate must not
        fail, or wait, because something downstream of it is missing. This
        breaker holds no opinion about which denials are worth an operator's
        attention, and it is not the place to start holding one.

        Runs once per subject per outage, on the same branch that writes the
        ledger file, so a gate asked in a loop reaches it once.

        Off the loop when there is one. Writing the item is two or three file
        operations, and this runs inside a gate that a spawn's done-callback
        asks on the event loop itself. Everything the write needs is read here,
        while it is still consistent, and handed over as plain values.
        """
        import asyncio

        try:
            from tesseract.orchestrator.autonomy.recovery import on_denial

            work = partial(
                on_denial,
                self.name,
                subject,
                error=self._last_error,
                # The outage, not the last trip in it.
                since=self._opened_at,
                retry_in=self.remaining_cooldown(),
                refused=tuple(self._denials),
            )
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                work()  # a scheduler, a script, a test: nothing to block
                return
            loop.run_in_executor(None, work)
        except Exception:
            logger.debug(
                "breaker '%s': could not report what it refused", self.name, exc_info=True,
            )

    def _rehydrate_from_log(self) -> None:
        # Without this, a fresh process loses the persisted "tripped" state:
        # the next successful call short-circuits in record_success and never
        # appends a "reset" event, leaving the JSONL (and the mirror UI that
        # reads it) stuck open forever.
        #
        # The remaining cooldown is restored, not the trip: a breaker that
        # opened for five minutes and was last written to yesterday rehydrates
        # ready to probe. Restoring it as freshly-open is what made a trip
        # survive a restart as a permanent one (invariants 2 and 3).
        if self._log_dir is None:
            return
        log_path = self._log_dir / f"{self.name}.jsonl"
        if not log_path.exists():
            return
        events: list[dict] = []
        with log_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
        trips = _trips_since_reset(events)
        last = events[-1] if events else None
        if last is None or last.get("event") != "tripped":
            return
        self.is_open = True
        self.failure_count = self.max_failures
        self._trips = trips
        self._last_error = str(last.get("error") or "")
        self._tripped_at = _read_time(last.get("timestamp"))
        # A trip written before `opened_at` existed says only when it happened,
        # and one trip is all the outage a reader of it can see.
        self._opened_at = _read_time(last.get("opened_at")) or self._tripped_at
        self._open_until = self._restored_open_until(last, trips)
        self._load_denials()

    def _restored_open_until(self, trip: dict, trips: int) -> datetime | None:
        recorded = trip.get("open_for_seconds")
        try:
            cooldown = self._cooldown_for(trips) if recorded is None else float(recorded)
        except (TypeError, ValueError):
            # A damaged line is not worth a boot failure in whatever subsystem
            # built this breaker. Fall back to what the policy says.
            cooldown = self._cooldown_for(trips)
        if cooldown <= 0:
            return None
        when = _read_time(trip.get("timestamp"))
        if when is None:
            # A trip we cannot date is one we cannot wait out. Allow the probe
            # now rather than latch on a damaged line — a failing mechanism
            # re-opens on the probe, a healed one closes (invariant 8).
            return self._now()
        return when + timedelta(seconds=cooldown)

    def _close(self, announce: bool = True) -> None:
        """Everything closing a breaker means, in one place.

        There were two ways to close one and they left different traces:
        `record_success` wrote the `reset` event and told nobody, `reset` told
        the bus and wrote nothing. A reader of the file and a listener on the
        bus then disagreed about whether a breaker was still open, which is the
        same split this whole plan exists to end.
        """
        self.failure_count = 0
        self._trips = 0
        self.is_open = False
        self._open_until = None
        self._last_error = ""
        self._tripped_at = None
        self._opened_at = None
        self._clear_denials()
        if announce:
            self._log_reset()
            self._publish("breaker_reset", {"name": self.name})

    def record_success(self) -> None:
        self.failure_count = 0
        self._trips = 0
        if self.is_open:
            logger.info("Circuit breaker '%s' auto-reset after successful call", self.name)
            self._close()

    def record_failure(self, error: str = "") -> None:
        self.failure_count += 1
        # `is_tripped`, not `is_open`: a probe that fails while the breaker is
        # open-but-elapsed has to re-open it, or the cooldown that already
        # expired lets the next caller straight back in.
        if self.failure_count < self.max_failures or self.is_tripped:
            return
        self._trips += 1
        cooldown = self._cooldown_for(self._trips)
        # ONE reading of the clock, used for every field and written to the
        # file unchanged. It was read again inside `_log_trip`, after a mkdir
        # and a file open, so the object and its own log disagreed by however
        # long that took. Anything keyed on the trip time then missed: the
        # recovery pass mints an outage's id from this field, and the watchman
        # re-derives that id from the file, so an owned fault read as unowned.
        now = self._now()
        self.is_open = True
        self._last_error = error[:ERROR_CAP]
        self._tripped_at = now
        # When this outage BEGAN, which is not the same as when it last
        # tripped. A probe that fails after the cooldown re-opens the breaker
        # without ever closing it, so the trip time walks forward through one
        # continuous outage, once per backoff cycle. Identity has to stand
        # still for as long as the breaker is open, or the write-up for the
        # outage is orphaned and a fresh one is written every few minutes.
        if self._opened_at is None:
            self._opened_at = now
        self._open_until = (
            now + timedelta(seconds=cooldown) if cooldown > 0 else None
        )
        logger.warning("Circuit breaker '%s' tripped after %d failures", self.name, self.failure_count)
        self._log_trip(error, cooldown, now)
        self._publish("breaker_trip", {
            "name": self.name,
            "failure_count": self.failure_count,
            "error": error[:ERROR_CAP],
        })

    def reset(self, announce: bool = True) -> None:
        """Close it because someone said so, rather than because a call worked.

        The operator's route in. Same trace as any other close, so the file,
        the bus and this object cannot end up telling three stories. `announce`
        is for the one caller closing several objects that share a name: one
        action, one `reset` event, one message on the bus.
        """
        self.failure_count = 0
        self._trips = 0
        if self.is_open:
            logger.info("Circuit breaker '%s' cleared", self.name)
            self._close(announce=announce)

    def _publish(self, kind: str, payload: dict) -> None:
        try:
            from tesseract.orchestrator.background_event_bus import get_background_bus
            get_background_bus().publish(kind, payload)
        except Exception:
            pass

    def _log_trip(self, error: str, cooldown: float, now: datetime) -> None:
        """`now` is handed in, never read again here: see `record_failure`."""
        if self._log_dir is None:
            return
        log_path = self._log_dir / f"{self.name}.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "event": "tripped",
            "breaker": self.name,
            "failures": self.failure_count,
            "error": error[:ERROR_CAP],
            # When this outage started, carried on every trip in it so a
            # reader of the file knows the same thing the object does.
            "opened_at": (self._opened_at or now).isoformat(),
            # How long this trip refuses for, so a reader (and the next
            # process) sees the decision that was made rather than recomputing
            # it from a config that may have changed since.
            "open_for_seconds": round(cooldown, 3),
            "timestamp": now.isoformat(),
        }
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def _log_reset(self) -> None:
        if self._log_dir is None:
            return
        log_path = self._log_dir / f"{self.name}.jsonl"
        entry = {
            "event": "reset",
            "breaker": self.name,
            "timestamp": self._now().isoformat(),
        }
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def load_breaker_statuses(log_dir: Path) -> list[dict]:
    """Return full status of every persisted circuit breaker.

    Each entry: {name, tripped, tripped_at, opened_at, error, reset_at,
    open_for_seconds}.
    `tripped` is the open question, not the refusing-right-now one: a breaker
    whose cooldown has elapsed is still open until a call closes it.
    """
    if not log_dir.exists():
        return []
    statuses = []
    for log_file in sorted(log_dir.glob("*.jsonl")):
        events: list[dict] = []
        with log_file.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        last_event = events[-1] if events else None
        last_trip = next((e for e in reversed(events) if e.get("event") == "tripped"), None)
        last_reset = next((e for e in reversed(events) if e.get("event") == "reset"), None)
        statuses.append({
            "name": log_file.stem,
            "tripped": bool(last_event and last_event.get("event") == "tripped"),
            "tripped_at": last_trip.get("timestamp") if last_trip else None,
            # When the outage began, as against when it last re-opened. The
            # two differ for every breaker that has been probing and failing,
            # and anything identifying an outage needs the one that stands
            # still. Falls back for a trip written before the field existed.
            "opened_at": (
                last_trip.get("opened_at") or last_trip.get("timestamp")
                if last_trip else None
            ),
            "error": last_trip.get("error", "") if last_trip else None,
            "reset_at": last_reset.get("timestamp") if last_reset else None,
            "open_for_seconds": last_trip.get("open_for_seconds") if last_trip else None,
            "trips_since_reset": _trips_since_reset(events),
        })
    return statuses


def load_tripped_breakers(log_dir: Path) -> dict[str, bool]:
    """Scan circuit breaker logs and return which breakers are currently open.

    Returns {breaker_name: True} for breakers with a trip event and no
    subsequent reset. Used at session startup to restore breaker state.
    """
    if not log_dir.exists():
        return {}
    tripped: dict[str, bool] = {}
    for log_file in log_dir.glob("*.jsonl"):
        breaker_name = log_file.stem
        last_event = None
        with log_file.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    last_event = json.loads(line)
                except json.JSONDecodeError:
                    continue
        if last_event and last_event.get("event") == "tripped":
            tripped[breaker_name] = True
    return tripped


#: `(name, log_dir)` -> the one breaker held for it. Strong references, and
#: safe because of the eviction below: this is a cache for ONE home at a time,
#: not a process-lifetime pile.
_HELD: dict[tuple[str, str], "CircuitBreaker"] = {}


def held_breaker(
    name: str,
    log_dir: Path | None = None,
    max_failures: int | None = None,
) -> "CircuitBreaker":
    """The process's one breaker for `name`, built on first ask.

    A breaker for a thing that has never failed does not need to exist, and a
    caller that built a fresh one per call would restart the count every time
    and never trip. So construction is lazy and the object is held — which is
    also what puts it in `_LIVE`, where `breaker_report` and `clear_breaker`
    find it, so a name nobody registered is still answerable from a phone.

    A home that moved makes every count against the old one meaningless, and
    holding those alive would let `breaker_report` merge them into a report
    about the NEW home. Dropping them is what makes the strong reference safe.

    Lifted from `orchestrator/repairs.py`, which had this exact cache for its
    own breakers and is now one of its callers: the tool funnel needed the same
    thing, and two copies of a cache whose whole subtlety is the eviction rule
    is how the two come to disagree about which home they are counting for.
    """
    directory = log_dir if log_dir is not None else _default_log_dir()
    cache_key = (name, str(directory))
    for stale in [k for k in _HELD if k[1] != cache_key[1]]:
        _HELD.pop(stale, None)
    held = _HELD.get(cache_key)
    if held is None:
        held = CircuitBreaker(name=name, max_failures=max_failures, log_dir=directory)
        _HELD[cache_key] = held
    return held


def reset_held_breakers() -> None:
    """Drop every held breaker. For tests, and for the reason `repairs.py`'s
    own reset exists: a suite that builds breakers against one temp home must
    not hand them to the next."""
    _HELD.clear()


def _default_log_dir() -> Path:
    from tesseract.paths import log_dir

    return log_dir("circuit-breakers")


def clear_breaker(name: str, log_dir: Path) -> str | None:
    """Close the breaker called `name`. Returns what happened, or None when
    there is no breaker by that name.

    Through the live objects wherever there are any, because a gate reads the
    object and appending a `reset` line to the file would leave the two
    disagreeing: the panel would show it closed and every wake would still be
    refused. All of them, not the newest: a subsystem that builds a breaker per
    run can have two alive, and closing one would report success while the
    other went on refusing. A name with nothing alive belongs to a subsystem
    not built in this process, and then the file is all there is to correct.
    """
    # Both kinds, and both BEFORE the file, because they are live objects and
    # a gate reads the object. Both rather than whichever is found first: a
    # name held by both would otherwise have half of it closed and be reported
    # as closed, which is the failure `clear_breaker` already refuses within
    # one kind. A chain entry's cooldown writes no file, so for that half the
    # live object is not merely the better answer, it is the only one.
    live = live_breakers(name)
    foreign = foreign_breakers(name)
    if live or foreign:
        open_ones = [breaker for breaker in live if breaker.is_open]
        open_foreign = [breaker for breaker in foreign if breaker.is_open_now()]
        if not open_ones and not open_foreign:
            return f"{name} was already closed"
        for position, breaker in enumerate(open_ones):
            breaker.reset(announce=position == 0)
        for breaker in open_foreign:
            breaker.close()
        if open_foreign and not open_ones:
            # `CircuitBreaker.reset` announces for itself; nothing else does.
            try:
                from tesseract.orchestrator.background_event_bus import get_background_bus
                get_background_bus().publish("breaker_reset", {"name": name})
            except Exception:
                pass
        return f"{name} is closed"
    # The name arrives from a tool argument, so it is only ever one this
    # directory already holds. A stem from `glob` cannot carry a path
    # separator, and matching against that set is what keeps a name like
    # `../../elsewhere` from being written to.
    persisted = {status["name"]: status for status in load_breaker_statuses(log_dir)}
    if name not in persisted:
        return None
    if not persisted[name]["tripped"]:
        return f"{name} was already closed"
    path = log_dir / f"{name}.jsonl"
    entry = {
        "event": "reset",
        "breaker": name,
        "timestamp": datetime.now().astimezone().isoformat(),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{json.dumps(entry)}\n")
    # Everything closing means, in the one path with no object to go through.
    # Leaving the ledger behind here would hand the NEXT outage the subjects of
    # this one: a later trip rehydrates and reads the file it was never told to
    # forget.
    try:
        denials_path(log_dir, name).unlink(missing_ok=True)
    except OSError:
        logger.exception("could not clear what breaker '%s' refused", name)
    try:
        from tesseract.orchestrator.background_event_bus import get_background_bus
        get_background_bus().publish("breaker_reset", {"name": name})
    except Exception:
        pass
    return f"{name} is closed. Nothing was holding it open in this process"


def _reported_row(status: dict, live: "CircuitBreaker | None", log_dir: Path,
                  now: datetime) -> dict:
    """One breaker as the operator needs to see it.

    A live object is asked directly, because it is the only thing that knows
    whether a gate would refuse right now. Without one the same two answers are
    derived from what the trip itself recorded: when it happened and how long
    it decided to refuse for. Reporting `is_open` as "refusing now" was wrong
    in exactly the case that matters most, a breaker persisted open across a
    restart whose wait has since run out.
    """
    row = dict(status)
    if live is not None:
        remaining = live.remaining_cooldown()
        row["refusing_now"] = live.is_tripped
        row["retry_in_seconds"] = None if remaining is None else round(remaining, 1)
        ledger = _merged_denials(status["name"])
    else:
        refusing, remaining = _refusing_from_file(status, now)
        row["refusing_now"] = refusing
        row["retry_in_seconds"] = remaining
        ledger = tuple(read_denials(denials_path(log_dir, status["name"])).values())
    row["refused"] = [
        {"subject": d.subject, "since": d.first_at.isoformat(), "times": d.count}
        for d in ledger
    ]
    return row


def _refusing_from_file(status: dict, now: datetime) -> tuple[bool, float | None]:
    """Whether the recorded trip is still refusing, and for how much longer."""
    if not status["tripped"]:
        return False, None
    when = _read_time(status["tripped_at"])
    recorded = status.get("open_for_seconds")
    # A trip written before `open_for_seconds` existed says only when it
    # happened, so the wait is recomputed the way the live object would have:
    # from the policy AND the trips since the last reset, because a repeat trip
    # waits longer and a flat first cooldown would report it as ready early.
    base, backoff, ceiling = _cooldown_policy()
    fallback = _cooldown_span(base, backoff, ceiling, status.get("trips_since_reset") or 1)
    try:
        cooldown = fallback if recorded is None else float(recorded)
    except (TypeError, ValueError):
        cooldown = fallback
    if when is None or cooldown <= 0:
        # Undatable, or a trip taken with the probe disabled. Both mean the
        # file cannot say when it tries again, and the object would have to.
        return True, None
    remaining = (when + timedelta(seconds=cooldown) - now).total_seconds()
    return remaining > 0, round(max(0.0, remaining), 1)


def breaker_report(log_dir: Path) -> list[dict]:
    """Every breaker, what it refused, and when it tries again.

    Both halves, because neither is the whole answer: the file holds every
    breaker that has ever tripped on this machine and survives a restart, and
    the live registry holds the ones running right now, including any whose
    subsystem never gave them a file to write to.
    """
    now = datetime.now().astimezone()
    rows: list[dict] = []
    persisted: set[str] = set()
    for status in load_breaker_statuses(log_dir):
        persisted.add(status["name"])
        rows.append(_reported_row(status, _representative(status["name"]), log_dir, now))
    for name in sorted(set(_LIVE) - persisted):
        live = _representative(name)
        if live is None:
            continue
        rows.append(_reported_row({
            "name": name,
            "tripped": live.is_open,
            "tripped_at": None,
            "error": None,
            "reset_at": None,
            "open_for_seconds": None,
        }, live, log_dir, now))
        persisted.add(name)
    for name in _foreign_names():
        # A name held by both kinds reports as the `CircuitBreaker`, because
        # that is the half with a file and a history behind it. `clear_breaker`
        # closes both regardless, so the control is right even where the row
        # is only half the story.
        if name in persisted:
            continue
        rows.append(_foreign_row(name))
    return rows


def _foreign_row(name: str) -> dict:
    """One foreign breaker in the shape every other row has.

    The open one decides, if any is: several can be alive for one name and the
    operator is asking about the door, not about which object is holding it.
    Nothing here is read from a file because these write none --- a chain
    entry's cooldown lives and dies with the session that holds it, which is
    why `tripped_at` is honestly absent rather than invented from the clock.
    """
    live = foreign_breakers(name)
    open_ones = [breaker for breaker in live if breaker.is_open_now()]
    speaking = open_ones[0] if open_ones else (live[0] if live else None)
    remaining = max((b.remaining_now() for b in open_ones), default=0.0)
    return {
        "name": name,
        "tripped": bool(open_ones),
        "refusing_now": bool(open_ones),
        "retry_in_seconds": round(remaining, 1) if open_ones else None,
        "tripped_at": None,
        "error": speaking.says() if speaking is not None else None,
        "reset_at": None,
        "open_for_seconds": None,
        "refused": [],
    }


def is_recovering(row: dict) -> bool:
    """Will this breaker try again without anyone doing anything?

    One reading, because three readers had one each: the watchman's source
    decided a breaker's severity with it, the judge decided whether to speak
    again with it, and the drift signal counted with it. They agreed today and
    were three places to correct tomorrow.

    A row that is not open is recovering by definition. A row with no probe
    coming is not: that is the one state a breaker cannot leave on its own.
    """
    if not row.get("tripped"):
        return True
    return row.get("retry_in_seconds") is not None


def row_for(log_dir: Path, name: str) -> dict | None:
    """One breaker's row out of the report, or None if it has never tripped."""
    return next((r for r in breaker_report(log_dir) if r["name"] == name), None)
