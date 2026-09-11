"""OutboundNotifier.

Single notify path for autonomous → operator pings. Replaces the two
hand-rolled rate-cap-exempt closures (Governor + UpgradeManager restart)
with a unified surface that:

* composes each kind into a `Message` that carries no channel's markup,
  and lets the channel it is going to do the reading,
* sends each kind to the channels `config/routing.yaml` names for it, all of
  them at once, so one channel refusing decides nothing for the others,
* honours per-(category, channel) sliding-window rate caps loaded from
  ``channels.yaml::<channel>.outbound_rate``,
* skips muted categories (union of ``channels.yaml::<channel>.muted_categories``
  and the dashboard-editable ``<HOME>/runtime/outbound-mutes.json``),
* lets EXEMPT categories (``recovery_summary``, ``crash_storm_latched``,
  ``awaiting_operator``) bypass the cap entirely: the operator MUST see
  those.

Which channel a kind reaches is the operator's, and it is read from the table
rather than decided here. No channel is named in this file: a destination is a
name in the table, and `integrations/_outbound.py` turns a name into the people
on it. The ``channel_name="telegram"`` default argument that used to stand in
for a routing decision is gone, and so is the import that reached into the
Telegram package for a sender.

Durable state lives at ``<TESSERACT_HOME>/runtime/outbound-rates.json``
(sliding window timestamps per ``(channel, category)``). Path resolution
is call-time so tests routing through ``monkeypatch.setenv("TESSERACT_HOME",
tmp_path)`` keep production runtime untouched.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO, Any, Awaitable, Callable, Iterator, Literal, Sequence

from tesseract.orchestrator.autonomy.broadcasts import BROADCASTS
from tesseract.orchestrator.autonomy.message import Message, compose, render_plain
from tesseract.paths import TESSERACT_HOME, runtime_dir

log = logging.getLogger(__name__)


NotificationCategory = Literal[
    "workspace_change_applied",
    # Was missing from this list while `BROADCASTS` carried it. The tuple
    # below is derived from the declaration, so the drift cost nothing at
    # runtime and would have cost a type check.
    "runtime_repaired",
    "awaiting_operator",
    "recovery_summary",
    "governor_pause",
    "crash_storm_latched",
    "runtime_report",
    "schedule_failed",
    "scheduled_task_result",
    "voice_lane_down",
]

#: Every kind, and it is derived from the declaration rather than repeated
#: beside it. The two lists were separate and could disagree; one of them was
#: also four entries longer than anything that could emit them.
CATEGORIES: tuple[NotificationCategory, ...] = tuple(BROADCASTS)  # type: ignore[assignment]

#: The kinds that ignore a mute and a rate cap. Declared per kind rather than
#: listed here, so "the operator must see this" is stated where the kind is.
EXEMPT_CATEGORIES: frozenset[NotificationCategory] = frozenset(
    kind for kind, spec in BROADCASTS.items() if spec.must_be_seen  # type: ignore[misc]
)

#: The kinds the per-hour cap does not apply to. Not the same set as the one
#: above and not a subset of it either: a kind can be ordinary enough to mute
#: and still be something the operator asked for on a cadence of their own,
#: which is its own rate limit. Muting still reaches everything here.
UNCAPPED_CATEGORIES: frozenset[NotificationCategory] = EXEMPT_CATEGORIES | frozenset(
    kind for kind, spec in BROADCASTS.items() if not spec.rate_capped  # type: ignore[misc]
)

DEFAULT_RATE_PER_HOUR = 6
DEFAULT_WINDOW_SECONDS = 3600


def _home() -> Path:
    override = os.environ.get("TESSERACT_HOME")
    return Path(override).resolve() if override else TESSERACT_HOME


def outbound_rates_path() -> Path:
    return runtime_dir() / "outbound-rates.json"


def outbound_mutes_path() -> Path:
    return runtime_dir() / "outbound-mutes.json"


def outbound_recent_path() -> Path:
    return runtime_dir() / "outbound-recent.json"


# How many sent messages the runtime keeps. Bounded by construction rather
# than by a retention window: the file is rewritten whole on every send, so it
# never grows and nothing has to age it. The panel shows the newest and the
# rest are there for a reader tracing what a night said.
RECENT_SENT_KEPT = 20
#: How much of a message the operator's own copy keeps. Every kind but one
#: is a ping about a single event and needs far less; the runtime report is
#: a list, and its record is a reminder of what was said rather than the
#: artefact, which is on disk and named in the message itself.
RECENT_SENT_MAX_CHARS = 512


@dataclass(frozen=True)
class NotifyResult:
    """Return value from :meth:`OutboundNotifier.notify`.

    ``sent`` is the count of operator chat_ids the body actually reached,
    summed over every channel the kind is routed to. ``skipped`` is the
    cap/mute/no-destination no-op shape so callers can log *why* a
    notification was dropped without parsing strings. ``reason`` holds the
    distinct reasons across those channels, and ``by_channel`` says which
    channel gave which, so a message that reached one of two says so.
    """

    category: NotificationCategory
    sent: int = 0
    skipped: bool = False
    reason: str = ""
    errors: int = 0
    by_channel: tuple[tuple[str, str], ...] = ()
    #: Recipients who got part of it and not the rest. Counted in `errors`
    #: too, because they did not get what was sent. Named separately because
    #: "did anything reach them" and "did all of it" are different questions,
    #: and a caller that needs the first should read a number rather than
    #: match a reason string.
    partial: int = 0


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _lock_refused(wait: bool) -> str:
    """Why the lock was not taken, in the words that send a reader the right way.

    A non-blocking attempt fails because another process HOLDS the lock, which
    is the case `wait=False` exists for and is nothing to do with the platform.
    Both used to log "the record lock is unavailable here", so a two-writer race
    during a crash-storm exit read as a machine that cannot lock at all.
    """
    if wait:
        return "outbound: the record lock is unavailable here"
    return (
        "outbound: another process holds the record lock; writing without it, "
        "so one row of what was sent can be lost"
    )


@contextmanager
def _across_processes(path: Path, *, wait: bool = True) -> Iterator[None]:
    """Hold `<path>.lock` for the whole read-modify-write, or fall open.

    `wait=False` gives up rather than queueing for it. The supervisor records
    what it said on its way out of a crash storm, and the blocking primitive
    below retries for about ten seconds before it raises: a process that is
    trying to exit must not wait that long for a bookkeeping row, least of all
    on a machine that is already misbehaving.

    More than one process sends on this machine: the supervisor writes its own
    kinds and the backend writes the rest. An atomic write keeps the file from
    tearing, and does nothing about two writers that both read the same twenty
    rows and both write nineteen of them back with their own on top. The lock
    is over the pair, not over the write.

    Falls open where the platform primitive is missing, on the same rule the
    writer itself follows: a message that reached the operator and was not
    written down is still a message that reached them.
    """
    handle: IO[bytes] | None = None
    locked = False
    lock_path = path.with_name(f"{path.name}.lock")
    try:
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(lock_path, "a+b")
        except OSError:
            yield
            return
        if sys.platform == "win32":
            try:
                import msvcrt

                msvcrt.locking(
                    handle.fileno(),
                    msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK,
                    1,
                )
                locked = True
            except OSError:
                log.warning(_lock_refused(wait))
            except ImportError:
                log.warning("outbound: the record lock is unavailable here")
        else:
            try:
                import fcntl

                flags = fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB
                fcntl.flock(handle.fileno(), flags)
                locked = True
            except OSError:
                log.warning(_lock_refused(wait))
            except ImportError:
                log.warning("outbound: the record lock is unavailable here")
        try:
            yield
        finally:
            if locked:
                try:
                    if sys.platform == "win32":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    log.exception("outbound: the record lock would not release")
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass


# -- Rate ledger ----------------------------------------------------------


@dataclass
class RateLedger:
    """Sliding-window ledger keyed by ``(channel, category)``.

    Each key holds a list of UTC ISO timestamps. ``register`` prunes
    entries older than ``window_seconds`` before appending. ``allowed``
    checks the post-prune count against ``cap``. Persistence is whole-file
    atomic-write — the ledger is small (8 categories × a few channels ×
    a handful of timestamps) so JSON serialisation cost stays trivial."""

    window_seconds: int = DEFAULT_WINDOW_SECONDS
    _data: dict[str, list[str]] = field(default_factory=dict)
    _loaded: bool = False

    def _key(self, channel: str, category: NotificationCategory) -> str:
        return f"{channel}::{category}"

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        path = outbound_rates_path()
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.exception("outbound: rates file unreadable; resetting")
            return
        windows = raw.get("windows")
        if isinstance(windows, dict):
            for key, stamps in windows.items():
                if isinstance(key, str) and isinstance(stamps, list):
                    self._data[key] = [str(s) for s in stamps if isinstance(s, str)]

    def _persist(self) -> None:
        _atomic_write_json(
            outbound_rates_path(),
            {"schema": 1, "windows": self._data},
        )

    def _prune(self, key: str, now: datetime) -> list[str]:
        cutoff = now - timedelta(seconds=self.window_seconds)
        kept: list[str] = []
        for stamp in self._data.get(key, ()):
            try:
                ts = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                kept.append(stamp)
        self._data[key] = kept
        return kept

    def count(self, channel: str, category: NotificationCategory, *, now: datetime | None = None) -> int:
        self._load()
        moment = now or datetime.now(timezone.utc)
        kept = self._prune(self._key(channel, category), moment)
        return len(kept)

    def allowed(
        self,
        channel: str,
        category: NotificationCategory,
        cap: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        return self.count(channel, category, now=now) < cap

    def register(
        self,
        channel: str,
        category: NotificationCategory,
        *,
        now: datetime | None = None,
    ) -> None:
        self._load()
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        key = self._key(channel, category)
        self._prune(key, moment)
        self._data[key].append(moment.isoformat())
        self._persist()


# -- Mute store -----------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def read_recent_sent() -> list[dict[str, Any]]:
    """What the runtime last said to the operator, newest first.

    Empty when it has said nothing on this machine, which is a real answer and
    not a missing one.
    """
    path = outbound_recent_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        log.warning("outbound: the recent-sent record at %s is unreadable", path)
        return []
    rows = raw.get("sent") if isinstance(raw, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def record_sent(
    category: str,
    text: str,
    channels: Sequence[str],
    *,
    now: datetime | None = None,
    wait_for_lock: bool = True,
) -> None:
    """Keep this message, dropping the oldest past `RECENT_SENT_KEPT`.

    Best effort on purpose: a message that reached the operator and could not
    be written down is still a message that reached them, and failing the send
    over its own record would be the wrong way round.

    `category` is a plain string rather than a `NotificationCategory`, because
    the brief and the offline path both record kinds that are routed but are
    not notification categories. It is only ever written out as text.

    `wait_for_lock=False` for a caller that is on its way out of the process.
    It keeps the same best-effort promise with a shorter one attached: it will
    not queue behind another writer to keep it, **and it therefore writes
    unlocked when another writer holds the lock**. That is the one exception to
    the cross-process guarantee above: two writers can interleave and one row
    can be lost. Chosen over blocking a process that is trying to exit.
    """
    when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    row = {
        "category": str(category),
        "at": when.isoformat(),
        "channels": [str(c) for c in channels],
        # What it actually said. The operator's own copy of their own
        # notification, on their own machine, and the only place it exists
        # after Telegram has it.
        "text": _clip(text, RECENT_SENT_MAX_CHARS),
    }
    path = outbound_recent_path()
    try:
        # Read and write under one lock. Apart, two processes sending together
        # keep one row and drop the other.
        with _across_processes(path, wait=wait_for_lock):
            kept = [row, *read_recent_sent()][:RECENT_SENT_KEPT]
            _atomic_write_json(path, {"schema": 1, "sent": kept})
    except Exception:  # noqa: BLE001 — the send already happened
        log.warning("outbound: could not record what was sent to the operator")


def read_runtime_mutes() -> dict[str, list[str]]:
    """Read the dashboard-editable mute file. Returns
    ``{channel: [category, …]}``; missing file → empty dict.
    """
    path = outbound_mutes_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.exception("outbound: runtime mutes unreadable; treating as empty")
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for channel, cats in raw.items():
        if isinstance(channel, str) and isinstance(cats, list):
            out[channel] = [str(c) for c in cats if isinstance(c, str)]
    return out


def write_runtime_mutes(mutes: dict[str, list[str]]) -> None:
    payload: dict[str, list[str]] = {}
    for channel, cats in mutes.items():
        payload[str(channel)] = sorted({str(c) for c in cats})
    _atomic_write_json(outbound_mutes_path(), payload)


# -- Notifier -------------------------------------------------------------


ChannelsConfigGetter = Callable[[], Any | None]
#: What an adapter is, from here: something that takes the composed message
#: and says how many people it reached. How that message READS, and what it
#: costs to send, are both the channel's own business, which is what keeps this
#: file free of any one channel's vocabulary.
Adapter = Callable[[Message], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class _Delivered:
    """What one channel did with one message.

    A named record rather than a tuple, because it grew a fifth field and the
    unpacking sites did not all grow with it: a partial delivery is neither a
    send nor a plain failure, and every reader of this has to see which it is.
    """

    channel: str
    sent: int
    errors: int
    reason: str
    partial: int


class OutboundNotifier:
    """Single notify path. Construct once per backend; pass into the
    Governor / UpgradeManager / recovery hook so every outbound path
    shares the same rate ledger + mute logic.

    Where a kind goes is `config/routing.yaml`, read through
    ``outbound_routing``. It used to be ``notify``'s ``channel_name`` default
    argument, which no caller ever passed.

    ``adapters`` overrides how a named channel is reached. Left out, a name
    resolves through the channel registry, which is the only way this class
    learns that Telegram exists at all."""

    def __init__(
        self,
        *,
        channels_config_getter: ChannelsConfigGetter,
        clock: Callable[[], datetime] | None = None,
        ledger: RateLedger | None = None,
        adapters: dict[str, Adapter] | None = None,
        routing_getter: Callable[[], Any | None] | None = None,
        chain_getter: Callable[[], Any] | None = None,
    ) -> None:
        self._adapters = dict(adapters or {})
        self._routing_getter = routing_getter
        self._channels_config_getter = channels_config_getter
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ledger = ledger or RateLedger()
        # How the opening line gets written (AR-18 item 4). Left out, nothing
        # is written and every message goes out phrased by its template, which
        # is where this path was before and is a working message either way.
        # A getter rather than a chain, because the config watcher rebuilds
        # adapters without a restart and a chain held from boot would go on
        # calling a model the operator has since moved off.
        self._chain_getter = chain_getter

    @property
    def ledger(self) -> RateLedger:
        return self._ledger

    async def _write_the_line(self, message: "Message", category: str) -> "Message":
        """The composed message, with its body written from its own counts.

        Wrapped here rather than inlined so the send path has one line for it
        and a chain that cannot be built is not a message that fails to go.
        """
        from tesseract.orchestrator.autonomy import outbound_writer

        if self._chain_getter is None:
            return message
        try:
            chain = self._chain_getter()
        except Exception:  # noqa: BLE001 — a message may not fail over its phrasing
            log.exception("outbound: could not build the writing chain for %s", category)
            return message
        return await outbound_writer.written(message, chain, subject=category)

    def _channel_block(self, name: str) -> Any | None:
        cfg = self._channels_config_getter() if self._channels_config_getter else None
        if cfg is None:
            return None
        block = getattr(cfg, "channel_block", None)
        if callable(block):
            return block(name)
        return getattr(cfg, name, None)

    def _muted(self, channel_name: str, category: NotificationCategory) -> bool:
        block = self._channel_block(channel_name)
        muted_yaml: list[str] = []
        if block is not None:
            raw = getattr(block, "muted_categories", None)
            if isinstance(raw, (list, tuple)):
                muted_yaml = [str(c) for c in raw]
        runtime = read_runtime_mutes().get(channel_name, [])
        return category in muted_yaml or category in runtime

    def _cap_for(self, channel_name: str, category: NotificationCategory) -> int:
        block = self._channel_block(channel_name)
        if block is None:
            return DEFAULT_RATE_PER_HOUR
        rate_block = getattr(block, "outbound_rate", None)
        if rate_block is None:
            return DEFAULT_RATE_PER_HOUR
        # Per-category override → global default → DEFAULT_RATE_PER_HOUR
        per_category = getattr(rate_block, "per_category", None) or {}
        if isinstance(per_category, dict) and category in per_category:
            try:
                return int(per_category[category])
            except (TypeError, ValueError):
                pass
        default_cap = getattr(rate_block, "default_per_hour", None)
        try:
            return int(default_cap) if default_cap is not None else DEFAULT_RATE_PER_HOUR
        except (TypeError, ValueError):
            return DEFAULT_RATE_PER_HOUR

    def _destinations(self, category: NotificationCategory) -> tuple[str, ...]:
        """The channels this kind goes to, from the operator's table.

        A broken file of THEIRS already degrades to the shipped rows inside
        `load_outbound_routing`, so what reaches this handler is a shipped
        table that cannot be read: an install with nothing left to fall back
        on. Nothing is sent, and the log says why."""
        routing = self._routing_getter() if self._routing_getter else None
        if routing is None:
            from tesseract.orchestrator.autonomy.outbound_routing import (
                load_outbound_routing,
            )

            try:
                routing = load_outbound_routing()
            except Exception:
                log.exception("outbound: routing table unreadable for %s", category)
                return ()
        return tuple(routing.destinations(category))

    def _adapter_for(self, channel: str) -> Adapter:
        override = self._adapters.get(channel)
        if override is not None:
            return override
        from tesseract.integrations._outbound import operator_sender

        return operator_sender(channel)

    def _refusal_for(
        self, category: NotificationCategory, channel: str, now: datetime
    ) -> str:
        """Why this channel will not take this message, or `""` when it will.

        ONE answer to the two guards, because two callers ask it and a second
        copy would drift: `_deliver` refuses on it, and `notify` asks whether
        ANY channel is going to take the message before paying a model to
        write its opening line. Without that second caller, a category muted
        everywhere still spent, and its ceiling could be emptied by messages
        nobody was ever going to read.

        Read-only. `RateLedger.allowed` counts and does not consume, so asking
        twice about one message cannot spend a slot; `register` is what
        consumes and only a real send calls it.
        """
        if category not in EXEMPT_CATEGORIES and self._muted(channel, category):
            return "muted"
        if category not in UNCAPPED_CATEGORIES:
            cap = self._cap_for(channel, category)
            if cap <= 0:
                return "cap_zero"
            if not self._ledger.allowed(channel, category, cap, now=now):
                return "rate_capped"
        return ""

    async def _deliver(
        self,
        category: NotificationCategory,
        message: Message,
        channel: str,
        now: datetime,
    ) -> tuple[str, int, int, str]:
        """One channel's share of a notification: ``(channel, sent, errors, reason)``.

        Two guards, because the mute and the cap answer different questions.
        Exempt categories bypass BOTH (GOVERNANCE §9: the operator MUST see
        crash-storm / recovery / awaiting-operator pings even if a dashboard
        toggle was flipped by accident). Uncapped ones bypass only the cap: a
        scheduled row's result is rate-limited by the cadence the operator
        chose, and one shared bucket per kind would drop the twentieth row
        because the first nineteen had fired. Muting still reaches it.

        Routing is a separate question and stays the operator's: a kind they
        sent to no channel is sent to no channel, exempt or not.
        """
        refused = self._refusal_for(category, channel, now)
        if refused:
            return _Delivered(channel, 0, 0, refused, 0)

        # Fail where it is used, not at load: a channel can be written into
        # the table before anything can speak it, and the sender says so.
        adapter = self._adapter_for(channel)
        try:
            result = await adapter(message)
        except Exception:
            log.exception(
                "outbound: sender raised for category=%s channel=%s", category, channel,
            )
            return _Delivered(channel, 0, 1, "sender_raised", 0)

        sent = int(result.get("sent", 0)) if isinstance(result, dict) else 0
        errors = int(result.get("errors", 0)) if isinstance(result, dict) else 0
        reason = str(result.get("reason") or "") if isinstance(result, dict) else ""
        # A partial delivery reached the operator and is not a clean send.
        # `fan_out` counts it as an error, correctly, and names it here so the
        # two things that follow from "did anything reach them" can still be
        # answered: the record of what they were told, and the warning that
        # nobody was.
        partial = int(result.get("partial", 0)) if isinstance(result, dict) else 0
        if reason and not sent and not partial:
            log.warning(
                "outbound: %s was routed to %r and reached nobody: %s",
                category, channel, reason,
            )
        if partial:
            log.warning(
                "outbound: %s reached %d recipient(s) on %r only in part",
                category, partial, channel,
            )
        if (sent > 0 or partial) and category not in UNCAPPED_CATEGORIES:
            self._ledger.register(channel, category, now=now)
        return _Delivered(channel, sent, errors, reason, partial)

    async def notify(
        self,
        category: NotificationCategory,
        context: dict[str, Any] | None = None,
        *,
        destinations: Sequence[str] | None = None,
    ) -> NotifyResult:
        """Send one kind, to the channels the table names for it.

        ``destinations`` is for a caller that owns a narrower answer than the
        table's: a schedule row saying where IT reports. Three states, and the
        difference between the last two is the whole point of the parameter
        being optional rather than defaulted:

        * ``None`` — the table decides. A row that says nothing about
          delivery follows the kind, so nothing has to be set twice.
        * a list of names — those channels instead, for this call only.
        * an EMPTY list — nowhere, deliberately. Same meaning an empty row
          has in the table.

        It replaces WHERE, never WHETHER. A mute, a rate cap and a
        safety-critical kind's exemption from both still apply inside every
        channel named here, exactly as they do to a channel the table named.
        """
        ctx = dict(context or {})
        if destinations is None:
            targets = self._destinations(category)
        else:
            named = (str(name).strip() for name in destinations)
            targets = tuple(dict.fromkeys(name for name in named if name))
        if not targets:
            return NotifyResult(category=category, skipped=True, reason="no_destination")

        message = compose(category, ctx)
        if not (message.title or message.body or message.bullets or message.facts):
            return NotifyResult(category=category, skipped=True, reason="empty_text")

        now = self._clock()
        # **The one gate** (ruling 32). Every rule about what may and may not
        # be written over lives in `outbound_writer`, not at the call sites:
        # a caller cannot forget a rule it was never asked to remember, and
        # `payload` in particular is a promise about the body that has to hold
        # for every category at once. It returns the message unchanged on any
        # refusal, so nothing below can tell whether a model was reached.
        #
        # Only when SOMETHING is going to take it. The mute and the cap are
        # per channel and live in `_deliver`, so writing first meant a
        # category muted everywhere still paid for a sentence nobody read.
        # The same `now` decides here and there, so the two cannot disagree
        # about a cap across the seconds the writing takes.
        if any(not self._refusal_for(category, channel, now) for channel in targets):
            message = await self._write_the_line(message, category)
        # One channel refusing, capping or failing must not decide anything
        # for the others: the whole point of a table with two names in a row
        # is that they are two answers, not one with a backup.
        outcomes = await asyncio.gather(
            *(self._deliver(category, message, channel, now) for channel in targets),
            return_exceptions=True,
        )

        sent = 0
        errors = 0
        partial = 0
        by_channel: list[tuple[str, str]] = []
        reasons: list[str] = []
        for channel, outcome in zip(targets, outcomes):
            if isinstance(outcome, BaseException):
                log.exception(
                    "outbound: delivery raised for category=%s channel=%s",
                    category, channel, exc_info=outcome,
                )
                errors += 1
                by_channel.append((channel, "delivery_raised"))
                reasons.append("delivery_raised")
                continue
            sent += outcome.sent
            errors += outcome.errors
            partial += outcome.partial
            by_channel.append((outcome.channel, outcome.reason or "sent"))
            if outcome.reason and outcome.reason not in reasons:
                reasons.append(outcome.reason)

        # Only what actually left the machine. A kind that was muted, capped
        # or routed nowhere said nothing to the operator, and a record of it
        # would be a record of a message they never got. A message that
        # arrived in part DID leave, and the operator has some of it in front
        # of them, so it is recorded on the channels that carried any of it.
        if sent > 0 or partial:
            # Plainly, not as any one channel read it. Two channels can
            # render the same message differently, and the operator's copy is
            # of what was SAID.
            record_sent(
                category,
                render_plain(message),
                [
                    name for name, why in by_channel
                    if why in {"sent", "partial_delivery"}
                ],
                now=now,
            )

        return NotifyResult(
            category=category,
            sent=sent,
            errors=errors,
            skipped=sent == 0 and errors == 0,
            reason=", ".join(reasons),
            by_channel=tuple(by_channel),
            partial=partial,
        )


__all__ = [
    "CATEGORIES",
    "UNCAPPED_CATEGORIES",
    "DEFAULT_RATE_PER_HOUR",
    "DEFAULT_WINDOW_SECONDS",
    "EXEMPT_CATEGORIES",
    "NotificationCategory",
    "NotifyResult",
    "OutboundNotifier",
    "RateLedger",
    "RECENT_SENT_KEPT",
    "RECENT_SENT_MAX_CHARS",
    "outbound_mutes_path",
    "outbound_rates_path",
    "outbound_recent_path",
    "read_recent_sent",
    "read_runtime_mutes",
    "record_sent",
    "write_runtime_mutes",
]
