"""Whether the machine was there, read from the machine's own record.

The runtime kept asking a question it had no way to answer: a scheduled row
that did not fire, a worker whose heartbeat is hours old, a gap in every log
at once. Was that the app, or was the computer asleep?

Until now it guessed. `rows.py::_stalls` rejected a window the scheduler fired
through more than once, which is a heuristic over one file, and worker recovery
called a heartbeat older than ninety seconds dead, which after a three hour
sleep is every heartbeat on the machine. Both read elapsed time and inferred a
cause from it.

**Windows already keeps the answer and this reads it.** The System event log
carries when the machine went to standby and came back, when a shutdown was
asked for and by which process, and whether the last one was clean. So the
runtime can say the app broke, or the person shut the machine down, or the
power went, and mean it.

Read in process through `win32evtlog`, not by shelling out, and never at
import. Everything here degrades to "I do not know", which callers must treat
as their old behaviour rather than as "nothing happened": a reader that cannot
see the power history has to keep blaming time, because the alternative is
excusing a real fault on no evidence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# The System log's own words for the machine going away and coming back.
#
# Two generations, because a laptop on Modern Standby and a desktop on S3 do
# not write the same ids, and a reader that knows only one of them reports a
# machine that never sleeps. 506/507 are Modern Standby, which is what this
# machine writes; 42/107 are the classic suspend and resume.
_SLEEP_IDS = {506, 42}
_WAKE_IDS = {507, 107}

# A shutdown somebody asked for. 1074 names the process that asked, which is
# how "the person chose to" is told from "Windows Update chose to".
_ASKED_TO_STOP = 1074
# The event log service stopping and starting, which brackets a clean stop.
_LOG_STOPPED = 6006
_LOG_STARTED = 6005
# The two ways the machine says the last stop was not clean. 6008 carries the
# moment it died; 41 is the kernel saying it came back without having been
# shut down. Either one means the gap was the machine, not the app.
_UNCLEAN_IDS = {6008, 41}

_ALL_IDS = _SLEEP_IDS | _WAKE_IDS | _UNCLEAN_IDS | {_ASKED_TO_STOP, _LOG_STOPPED, _LOG_STARTED}

# How near a boot an unclean-restart row has to be to belong to THAT boot.
#
# Measured, not chosen: 180 days of this machine's System log, every 6005,
# 6008 and 41 row in it.
#
#   Within one unclean boot the rows span 16.8s at the widest. Both
#   occurrences look the same: 41, then 6008 about 16.5s later, then 6005 one
#   millisecond after 6008.
#   Between two DISTINCT boots the closest measured separation is 58.5s, on a
#   night with three restarts inside four minutes.
#
# So the bound has to sit above 16.8 and below 58.5, and thirty seconds is the
# middle of that. Sixty would merge that pair of restarts into one boot and
# hand the second one's power failure to the first, which is the same class of
# error as the one this whole binding exists to stop.
_SAME_BOOT = timedelta(seconds=30)

# What a gap is called, in words a person reads rather than an event id.
ASLEEP = "asleep"
SHUT_DOWN = "shut down"
LOST_POWER = "lost power"

# How long one read of the event log may wait. `EvtNext` takes -1 for INFINITE,
# which is what this had first, and an infinite wait is the one thing this
# module promised not to do: it is read on the watchman's tick and at boot for
# every worker record, so an Event Log service that is wedged would take the
# sweep and the recovery pass with it. Everything else here degrades to "I do
# not know"; this makes the read do the same instead of hanging.
_READ_TIMEOUT_MS = 5_000

# How long a shutdown request still explains the stop that follows it. Windows
# logs the request (1074) and the stop separately, and a request nobody
# completed leaves the first without the second: `shutdown /a` cancels one, an
# application can block one, and a person can change their mind. Ten minutes is
# the outer edge of Windows' own shutdown countdown, so a stop later than this
# is a different shutdown and is reported as one nobody was recorded asking
# for, which is the honest answer rather than the previous name.
_REQUEST_STANDS_FOR = timedelta(minutes=10)

# How far back a reader looks by default. It lives here rather than at each
# call because it answers a question about THIS record: an absence is only
# reported once both its ends are in what was read, so the window has to reach
# back past the start of any sweep or a night's sleep is invisible to the
# report written the morning after it. Two callers had a private copy of the
# same number, which is two definitions of one fact.
LOOKBACK = timedelta(days=7)

@dataclass(frozen=True)
class Gap:
    """One stretch the machine was not running, and why."""

    began: datetime
    ended: datetime
    kind: str
    # Who asked, where the record says. Empty when nobody did.
    asked_by: str = ""

    @property
    def seconds(self) -> float:
        return max(0.0, (self.ended - self.began).total_seconds())

    def covers(self, start: datetime, end: datetime) -> bool:
        """Does this gap account for the whole of `start` to `end`?

        Both ends, deliberately. Something that began while the machine was
        away and is STILL going now is a fault the gap merely precedes, which
        is the same rule the judge's boot and outage windows already keep.
        """
        return self.began <= start and end <= self.ended

    def said(self) -> str:
        if self.kind == ASLEEP:
            return f"the machine was asleep for {_plainly(self.seconds)}"
        if self.kind == SHUT_DOWN:
            who = f", asked for by {self.asked_by}" if self.asked_by else ""
            return f"the machine was shut down for {_plainly(self.seconds)}{who}"
        return f"the machine lost power for {_plainly(self.seconds)}"


def _plainly(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)} seconds"
    if seconds < 5400:
        return f"{seconds / 60:.0f} minutes"
    return f"{seconds / 3600:.1f} hours"


def _rows(since: datetime, now: datetime):
    """Every power row the System log holds since `since`, newest first.

    Raises. The caller decides what not knowing means, and every caller here
    has a different right answer, so this does not choose one for them.
    """
    import win32evtlog

    query = win32evtlog.EvtQuery(
        "System",
        win32evtlog.EvtQueryChannelPath | win32evtlog.EvtQueryReverseDirection,
        _query_for(since, now),
        None,
    )
    context = win32evtlog.EvtCreateRenderContext(win32evtlog.EvtRenderContextSystem)
    try:
        while True:
            handles = win32evtlog.EvtNext(query, 64, _READ_TIMEOUT_MS, 0)
            if not handles:
                return
            # The WHOLE BATCH is released, not just the row being read. Each
            # row is a handle, up to 64 of them, and a per-row `finally` alone
            # closes only the one in hand: a single row this cannot render
            # takes the generator down and the other 63 are already out of
            # `EvtNext` and never closed. That is the same leak the per-row
            # close was added for, reached from the failure side, and this
            # read runs on the watchman's tick.
            try:
                for handle in handles:
                    values = win32evtlog.EvtRender(
                        handle, win32evtlog.EvtRenderEventValues, Context=context,
                    )
                    event_id = int(values[win32evtlog.EvtSystemEventID][0])
                    yield (
                        event_id,
                        _as_utc(values[win32evtlog.EvtSystemTimeCreated][0]),
                        _who_asked(handle) if event_id == _ASKED_TO_STOP else "",
                    )
            finally:
                for handle in handles:
                    _close(handle)
    finally:
        # Both are Windows handles this function opened. They would be released
        # when the PyHANDLE is collected, which is a promise about a garbage
        # collector rather than about this code, and the early return above and
        # the raise below both leave without reaching it.
        _close(query)
        _close(context)


def _close(handle) -> None:
    """Release one Windows handle, never raising. Closing is best effort by
    nature: the read is already over by the time it matters."""
    try:
        handle.Close()
    except Exception:  # noqa: BLE001
        pass


def _who_asked(handle) -> str:
    """Which program asked for the shutdown, in one word, and nothing else.

    1074's payload carries `param1` as `C:\\WINDOWS\\system32\\winlogon.exe
    (MACHINE)`, which is what makes "the person chose to" tellable from
    "Windows Update chose to". It also carries the machine name and the signed
    in account, in `param2` and `param7`, and this record is written to disk
    and rendered on a panel.

    So only the executable's own name is taken, and the known ones are turned
    into plain words. Nothing here keeps a path, a machine name or an account:
    the question is who asked, and the answer to that is not a person's
    username.
    """
    import re

    import win32evtlog

    try:
        # The XML render, and it is the SECOND render of this handle: `_rows`
        # already rendered it for its system fields. Kept, because the values
        # render needs a user context built from the publisher's own template
        # and 1074 rows are a handful a week, so a second render of those is
        # cheaper than carrying a second context through every row.
        xml = win32evtlog.EvtRender(handle, win32evtlog.EvtRenderEventXml)
        match = re.search(r"<Data Name=['\"]param1['\"]>([^<]*)</Data>", xml)
        if not match:
            return _SOMEONE
        program = match.group(1).split("(")[0].strip().replace("\\", "/").rsplit("/", 1)[-1]
    except Exception:  # noqa: BLE001 — a name is never worth failing the read for
        return _SOMEONE
    return _WHO.get(program.lower(), program or _SOMEONE)


# What the shutdown was asked for BY, where the name is one this runtime can
# put into words. Anything else keeps its own executable name, which is more
# use to a person than a category it was guessed into.
_SOMEONE = "someone at the machine"
_WHO = {
    "winlogon.exe": _SOMEONE,
    "explorer.exe": _SOMEONE,
    "trustedinstaller.exe": "Windows Update",
    "wuauclt.exe": "Windows Update",
    "musnotification.exe": "Windows Update",
    "svchost.exe": "Windows itself",
}


def _query_for(since: datetime, now: datetime) -> str:
    """The ids we care about, filtered in the log rather than in Python.

    A System log holds hundreds of thousands of rows and this runs on a tick,
    so the selection belongs on the other side of the call.
    """
    ids = " or ".join(f"EventID={i}" for i in sorted(_ALL_IDS))
    ms = max(1, int((now - since).total_seconds() * 1000))
    return (
        f"*[System[({ids}) and TimeCreated[timediff(@SystemTime) <= {ms}]]]"
    )


def _as_utc(value) -> datetime:
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return stamp.astimezone(timezone.utc) if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def gaps(since: datetime, *, now: datetime | None = None) -> list[Gap] | None:
    """Every stretch the machine was away since `since`, oldest first.

    `None` means the record could not be read, which is NOT the same as an
    empty list. Empty says the machine was up the whole time and a caller may
    stop blaming the machine; `None` says nothing is known and a caller must
    keep whatever rule it had.
    """
    end = now or datetime.now(timezone.utc)
    try:
        rows = sorted(_rows(since, end), key=lambda row: row[1])
    except Exception:  # noqa: BLE001 — the caller decides what not knowing costs
        log.debug("machine state: the power record could not be read", exc_info=True)
        return None

    out: list[Gap] = []
    away_since: datetime | None = None
    away_kind = ASLEEP
    asked_by = ""
    asked_at: datetime | None = None
    # Every unclean-restart row up front, so nothing downstream depends on
    # WHEN one was read relative to the boot it describes.
    #
    # This is the fourth design of this binding and the first that is
    # order-independent by construction rather than tolerant of the orders
    # anyone thought to test. The five properties are here rather than in a
    # commit message because three previous versions each satisfied the ones
    # in front of them and broke one of the others:
    #
    #   1. An unclean row speaks only for the boot it belongs to.
    #   2. An outage nothing bracketed is not invented. Explaining less is the
    #      safe direction, the same ruling as the unmatched wake below.
    #   3. The answer does not depend on which of a boot's rows was read
    #      first. On this machine 41 precedes 6005 by about 16.5 SECONDS of
    #      wall clock on every unclean boot in 180 days, so this is the normal
    #      ordering rather than a tie-break artefact.
    #   4. 6008 and 41 both arriving says the same as either one alone.
    #   5. A gap closed long ago is never reachable, however few rows follow.
    #
    # Every stateful version failed 5, and the last one failed it in a way its
    # own tests could not see. Carrying "the row before this closed a gap"
    # cannot work, because in the history that breaks it there are NO rows
    # between the two: a clean shutdown closed by a boot on Monday, then an
    # unbracketed power loss on Wednesday whose 41 is the very next row. No
    # sequence rule can separate those. Only the clock can, which is what
    # `_SAME_BOOT` is for and why it had to be measured.
    unclean_at = [when for event_id, when, _said in rows if event_id in _UNCLEAN_IDS]
    for event_id, when, said_by in rows:
        if event_id in _SLEEP_IDS:
            away_since, away_kind = when, ASLEEP
        elif event_id == _ASKED_TO_STOP:
            # Last request wins, and it is remembered WITH ITS MOMENT. A
            # request that is never followed by a stop is a shutdown somebody
            # aborted, and without the moment it sat here indefinitely and got
            # attached to whatever stop came next, hours later. A record that
            # names the wrong asker is worse than one that names nobody.
            asked_by, asked_at = said_by or _SOMEONE, when
        elif event_id == _LOG_STOPPED:
            away_since, away_kind = when, SHUT_DOWN
            if asked_at is None or (when - asked_at) > _REQUEST_STANDS_FOR:
                # Cleared, and the two cases read the same afterwards: no
                # request was logged at all, and one was logged too long
                # before this stop to be its cause. Saying which would be
                # better and needs a real delayed shutdown to measure first;
                # ten minutes is the outer edge of Windows' own countdown
                # rather than something observed on this machine.
                asked_by, asked_at = "", None
        elif event_id in _WAKE_IDS or event_id == _LOG_STARTED:
            # A wake whose sleep is not in the window is NOT a gap back to the
            # start of it. That reading was written first and it is exactly
            # backwards: the beginning is unknown, so filling it in with the
            # edge of what was read invents an absence, and the judge then
            # excuses every fault sitting inside the invention. Measured
            # against a planted worker failure that vanished from the report
            # because a wake eleven days later manufactured a window over it.
            #
            # Explaining less is the safe direction here. An unexplained fault
            # is still a fault and still reaches the operator; a wrongly
            # explained one is gone.
            if away_since is not None and when > away_since:
                # The kind is settled HERE, where the gap is closed, rather
                # than edited afterwards by whichever row happened to follow.
                #
                # Only a boot can be reclassified. A wake closes a gap just as
                # well and says nothing about whether the machine STARTED,
                # which is the only thing 6008 and 41 report on, so a resume
                # is never eligible however near an unclean row falls.
                kind = away_kind
                if event_id == _LOG_STARTED and any(
                    abs(u - when) <= _SAME_BOOT for u in unclean_at
                ):
                    kind = LOST_POWER
                out.append(Gap(
                    began=away_since, ended=when, kind=kind,
                    # No requester survives this. Losing power is the one kind
                    # nobody asked for, and carrying a name onto it would say a
                    # person or an update did what the power did.
                    asked_by=asked_by if kind == SHUT_DOWN else "",
                ))
            away_since, asked_by, asked_at = None, "", None
        # An unclean row needs no branch of its own any more. It opens no gap
        # and closes none: it only says that a boot near it was not a clean
        # one, and `unclean_at` above carries that to the boot that reads it.
        # An unclean row with no boot within `_SAME_BOOT` describes an outage
        # nothing bracketed, and nothing is invented for it.

    # Still away at the end of the window: the machine is asleep right now, or
    # the log stops mid-gap. Neither can be true of a process that is running
    # this code, so it is not reported as an open gap.
    return _joined(out)


# The longest "awake" stretch that was never awake. Modern Standby surfaces for
# its own maintenance and goes straight back down: measured on this machine,
# a wake at 04:06:30.525 and a sleep at 04:06:30.527, two milliseconds apart,
# eight times over two days.
#
# This is not a liveness decision and no clock decides anything with it. It is
# the resolution at which the record can tell one absence from two, and without
# it a gap arrives as a chain of touching pieces: `covers` then answers no for
# any window straddling a blip, so a row that did not fire at three in the
# morning would be blamed on the app because the machine woke for two
# milliseconds at one.
_NEVER_REALLY_AWAKE_S = 1.0


def _joined(found: list[Gap]) -> list[Gap]:
    """One absence, however many times the machine twitched inside it."""
    out: list[Gap] = []
    for gap in found:
        last = out[-1] if out else None
        if (
            last is not None
            and (gap.began - last.ended).total_seconds() <= _NEVER_REALLY_AWAKE_S
        ):
            # The stronger reading wins the joined stretch: a machine that lost
            # power inside what looked like a sleep did not have a nap.
            kind = LOST_POWER if LOST_POWER in (last.kind, gap.kind) else last.kind
            out[-1] = Gap(
                began=last.began, ended=gap.ended, kind=kind,
                asked_by="" if kind == LOST_POWER else (last.asked_by or gap.asked_by),
            )
            continue
        out.append(gap)
    # An absence shorter than the twitch that joins two of them is not an
    # absence. It is the same Modern Standby surfacing seen from the other
    # side, it explains nothing anybody would ask about, and it is a line in a
    # report that costs a reader a second to dismiss.
    return [gap for gap in out if gap.seconds > _NEVER_REALLY_AWAKE_S]


def explains(start: datetime, end: datetime, *, known: list[Gap] | None) -> Gap | None:
    """The gap that accounts for `start` to `end`, if the machine's own record
    has one. `known` is passed in rather than read, so a caller checking fifty
    workers reads the log once."""
    for gap in known or ():
        if gap.covers(start, end):
            return gap
    return None


def away_for(seconds_before: float, at: datetime, *, known: list[Gap] | None) -> float:
    """How much of the `seconds_before` leading up to `at` the machine was away.

    This is what turns an age into a fact. A heartbeat three hours old on a
    machine that slept for three hours is a heartbeat that was touched right
    before the lid closed, and subtracting the sleep is what says so.
    """
    if not known:
        return 0.0
    start = at - timedelta(seconds=seconds_before)
    away = 0.0
    for gap in known:
        began, ended = max(gap.began, start), min(gap.ended, at)
        if ended > began:
            away += (ended - began).total_seconds()
    return away


__all__ = [
    "ASLEEP", "Gap", "LOST_POWER", "SHUT_DOWN",
    "away_for", "explains", "gaps",
]
