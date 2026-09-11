"""Governor — the stop-digging detector layer.

Three deterministic detectors run on cadence + on-demand:

1. **Loop detector.** The same ``(source, goal)`` pair produced N times
   within window W → pause the source. Catches mappers that keep
   re-proposing identical work because their dedupe window is too short
   or because the operator keeps rejecting the same suggestion.
2. **Cost spiral detector.** A worker whose actual spend ≥ ``threshold``×
   the agenda item's budget cap → cancel the worker + transition the
   item to ``blocked`` with reason ``cost_spiral``. Catches a runaway
   model loop before it eats the daily cap.
3. **Trust degradation detector.** N consecutive operator rejections
   (``awaiting_operator`` → ``cancelled`` / ``abandoned``) from a single
   source → pause that source. Catches a noisy signal whose proposals
   the operator keeps killing.

Pauses persist to ``<HOME>/agenda/source-pauses.json`` (atomic write;
restart-safe) and append a JSONL audit row to
``<HOME>/logs/governor/pauses.jsonl``. The kernel reads pause state via
the ``PauseStore`` on boot + on every detector trigger so a pause set
on tick N is honoured on tick N+1.

**A pause lifts itself.** Each detector has a time in ``agenda.yaml`` and
the governor clears an expired pause at the top of its own tick, logging
``unpause`` with ``by: expiry``. The operator's REST lift stays as the early
one. This was the other half of stopping to dig: a detector that parks a
source and waits for a person turns a bad hour into a dead capability, and
the ``operator_view`` pause sat for twenty days.

**A source that comes straight back waits longer, and then asks.** The store
remembers the last lift per source: a pause inside the time it just served
doubles, to a cap, and the count that reaches ``advice_after`` files the
card that says the decision is the operator's. Backing off for ever is how a
loop nobody is told about becomes permanent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    dedupe_key,
)
from tesseract.orchestrator.autonomy.paths import (
    agenda_archive_dir,
    governor_log_path,
    source_pauses_path,
)
from tesseract.orchestrator.workers.record import (
    RiskClass,
    TERMINAL_STATUSES as WORKER_TERMINAL_STATUSES,
    WorkerRecord,
    WorkerStatus,
    list_active_records,
    write_record,
)
from tesseract.orchestrator.autonomy import journal as operator_journal

log = logging.getLogger(__name__)


DEFAULT_CADENCE_SECONDS = 60.0
DEFAULT_LOOP_N = 3
DEFAULT_LOOP_WINDOW_HOURS = 24
DEFAULT_COST_MULTIPLIER = 2.0
DEFAULT_TRUST_CONSECUTIVE = 3


REASON_LOOP_DETECTED = "loop_detected"
REASON_COST_SPIRAL = "cost_spiral"
REASON_TRUST_DEGRADED = "trust_degraded"
REASON_OPERATOR_UNPAUSE = "operator_unpause"
#: What the audit row says when the pause ran out rather than being lifted.
REASON_EXPIRED = "pause_expired"

DETECTOR_LOOP = "loop"
DETECTOR_COST_SPIRAL = "cost_spiral"
DETECTOR_TRUST_DEGRADATION = "trust_degradation"

#: Who lifted a pause, in the audit row and in the broadcast. Two words, and
#: they are not interchangeable: the operator deciding a source is fine again
#: starts the count over, and the clock running out does not.
BY_OPERATOR = "operator"
BY_EXPIRY = "expiry"


@dataclass(frozen=True)
class PausePolicy:
    """How long a pause lasts, and what a repeat costs.

    Indexed out of `agenda.yaml` rather than defaulted, per the project rule
    that config is the source of truth: a default here is a second answer to
    how long a capability stays off, and the one that would be believed is
    whichever was read last.

    `None` from `base_for` means this detector's pause does not expire, which
    is what every pause did before this existed and is the behaviour a
    detector with no row keeps.
    """

    ttl_seconds: dict[str, float] = field(default_factory=dict)
    backoff_multiplier: float = 1.0
    max_ttl_seconds: float = 0.0
    advice_after: int = 0

    def base_for(self, detector: str) -> float | None:
        seconds = self.ttl_seconds.get(detector)
        return seconds if seconds and seconds > 0 else None

    @classmethod
    def from_governor_dict(cls, gov: dict[str, Any]) -> "PausePolicy":
        pause = gov.get("pause")
        if not pause:
            # A config written before pauses expired. Nothing expires, which
            # is the old behaviour exactly rather than a guessed timeout.
            return cls()
        hours = pause["ttl_hours"] or {}
        return cls(
            ttl_seconds={
                str(detector): float(value) * 3600.0
                for detector, value in hours.items()
            },
            backoff_multiplier=float(pause["backoff_multiplier"]),
            max_ttl_seconds=float(pause["max_ttl_hours"]) * 3600.0,
            advice_after=int(pause["advice_after"]),
        )


@dataclass(frozen=True)
class GovernorConfig:
    cadence_seconds: float = DEFAULT_CADENCE_SECONDS
    loop_n: int = DEFAULT_LOOP_N
    loop_window_hours: int = DEFAULT_LOOP_WINDOW_HOURS
    cost_threshold_multiplier: float = DEFAULT_COST_MULTIPLIER
    trust_consecutive_rejections: int = DEFAULT_TRUST_CONSECUTIVE
    pause: PausePolicy = field(default_factory=PausePolicy)

    @classmethod
    def from_yaml_dict(cls, raw: dict[str, Any]) -> "GovernorConfig":
        gov = raw.get("governor") or {}
        loop = gov.get("loop") or {}
        cost = gov.get("cost_spiral") or {}
        trust = gov.get("trust_degradation") or {}
        return cls(
            cadence_seconds=float(gov.get("cadence_seconds", DEFAULT_CADENCE_SECONDS)),
            loop_n=int(loop.get("n", DEFAULT_LOOP_N)),
            loop_window_hours=int(loop.get("window_hours", DEFAULT_LOOP_WINDOW_HOURS)),
            cost_threshold_multiplier=float(
                cost.get("threshold_multiplier", DEFAULT_COST_MULTIPLIER)
            ),
            trust_consecutive_rejections=int(
                trust.get("consecutive_rejections", DEFAULT_TRUST_CONSECUTIVE)
            ),
            pause=PausePolicy.from_governor_dict(gov),
        )


@dataclass
class SourcePause:
    """One source's pause state. Serialised to ``source-pauses.json``."""

    source: AgendaSource
    paused_at: datetime
    detector: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)
    #: When this pause lifts itself. `None` is a pause that waits for the
    #: operator, which is every pause written before this field existed and
    #: every detector `PausePolicy` has no row for.
    expires_at: datetime | None = None
    #: How many pauses this is in an unbroken run of them, counting this one.
    #: 1 is a first pause, and a run is broken by the source behaving for
    #: longer than its own last wait or by the operator lifting it.
    consecutive: int = 1
    #: When the run started, so the card filed at `advice_after` is one card
    #: for one run rather than a new one per pause after the third.
    chain_started_at: datetime | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "paused_at": self.paused_at.isoformat(),
            "detector": self.detector,
            "reason": self.reason,
            "evidence": self.evidence,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "consecutive": self.consecutive,
            "chain_started_at": (
                self.chain_started_at.isoformat() if self.chain_started_at else None
            ),
        }

    @classmethod
    def from_payload(cls, raw: dict[str, Any]) -> "SourcePause | None":
        try:
            source = AgendaSource(raw["source"])
        except (KeyError, ValueError):
            return None
        paused_at = _read_time(raw.get("paused_at"))
        if paused_at is None:
            return None
        return cls(
            source=source,
            paused_at=paused_at,
            detector=str(raw.get("detector", "")),
            reason=str(raw.get("reason", "")),
            evidence=raw.get("evidence") or {},
            expires_at=_read_time(raw.get("expires_at")),
            consecutive=int(raw.get("consecutive") or 1),
            chain_started_at=_read_time(raw.get("chain_started_at")) or paused_at,
        )


@dataclass(frozen=True)
class RecentLift:
    """What the store remembers about a source between pauses.

    Only an EXPIRY writes one. An operator lifting a pause is a decision that
    the source is fine, so it clears the run rather than counting against it:
    the backoff is for a source the runtime keeps having to park on its own.
    """

    lifted_at: datetime
    consecutive: int
    ttl_seconds: float
    chain_started_at: datetime

    def to_payload(self) -> dict[str, Any]:
        return {
            "lifted_at": self.lifted_at.isoformat(),
            "consecutive": self.consecutive,
            "ttl_seconds": self.ttl_seconds,
            "chain_started_at": self.chain_started_at.isoformat(),
        }

    @classmethod
    def from_payload(cls, raw: dict[str, Any]) -> "RecentLift | None":
        lifted_at = _read_time(raw.get("lifted_at"))
        if lifted_at is None:
            return None
        return cls(
            lifted_at=lifted_at,
            consecutive=int(raw.get("consecutive") or 1),
            ttl_seconds=float(raw.get("ttl_seconds") or 0.0),
            chain_started_at=_read_time(raw.get("chain_started_at")) or lifted_at,
        )


def _read_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    try:
        tmp.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _append_log(row: dict[str, Any]) -> None:
    path = governor_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except OSError:
        log.exception("governor: pauses.jsonl append failed")


class PauseStore:
    """Durable source-pause registry — the canonical answer to *which
    sources are currently parked*. Reads + writes
    ``<HOME>/agenda/source-pauses.json`` at call time so test fixtures
    that ``monkeypatch.setenv("TESSERACT_HOME", tmp_path)`` route writes
    cleanly. Construct once per backend (the kernel + governor share
    the same instance).

    The optional ``broadcast_hook`` fans out
    ``governor_pause_added`` / ``governor_pause_removed`` envelopes so the
    Mirror Autonomy tab refreshes immediately. Mirror server boot wires
    the hook; REPL / standalone contexts leave it unset.
    """

    def __init__(
        self,
        *,
        broadcast_hook: Callable[[str, dict[str, Any]], None] | None = None,
        policy: "PausePolicy | None" = None,
    ) -> None:
        self._cache: dict[AgendaSource, SourcePause] | None = None
        self._recent: dict[AgendaSource, RecentLift] = {}
        self._broadcast_hook = broadcast_hook
        # Settable after construction because `routes/agenda::register` builds
        # a store before the config file has been read. No policy means no
        # pause expires, which is the behaviour every pause had before this.
        self._policy = policy or PausePolicy()

    def set_broadcast_hook(
        self,
        hook: Callable[[str, dict[str, Any]], None] | None,
    ) -> None:
        self._broadcast_hook = hook

    def set_policy(self, policy: "PausePolicy") -> None:
        self._policy = policy

    @property
    def policy(self) -> "PausePolicy":
        return self._policy

    def _fire_broadcast(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._broadcast_hook is None:
            return
        try:
            self._broadcast_hook(event_type, payload)
        except Exception:
            log.exception(
                "governor broadcast hook raised on %s; non-fatal", event_type,
            )

    def _load(self) -> dict[AgendaSource, SourcePause]:
        if self._cache is not None:
            return self._cache
        out: dict[AgendaSource, SourcePause] = {}
        recent: dict[AgendaSource, RecentLift] = {}
        path = source_pauses_path()
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.exception("governor: source-pauses.json unreadable; treating as empty")
                raw = {}
            for entry in (raw.get("pauses") or []):
                if not isinstance(entry, dict):
                    continue
                pause = SourcePause.from_payload(entry)
                if pause is not None:
                    out[pause.source] = pause
            for name, entry in (raw.get("recent") or {}).items():
                if not isinstance(entry, dict):
                    continue
                try:
                    source = AgendaSource(name)
                except ValueError:
                    continue
                lift = RecentLift.from_payload(entry)
                if lift is not None:
                    recent[source] = lift
        self._cache = out
        self._recent = recent
        return out

    def _persist(self) -> None:
        pauses = self._load()
        payload = {
            "pauses": [p.to_payload() for p in pauses.values()],
            # What each source did last, so a run of pauses survives the pause
            # itself being gone. Written beside them rather than in a second
            # file: they are one fact about a source and two files would need
            # a rule about which of them is right.
            "recent": {
                source.value: lift.to_payload()
                for source, lift in self._recent.items()
            },
        }
        _atomic_write_json(source_pauses_path(), payload)

    def reload(self) -> dict[AgendaSource, SourcePause]:
        """Drop the in-memory cache and re-read from disk. Called on
        kernel boot so a pause persisted in a previous process is honoured."""
        self._cache = None
        return self._load()

    def all_paused(self) -> dict[AgendaSource, SourcePause]:
        return dict(self._load())

    def is_paused(self, source: AgendaSource) -> bool:
        return source in self._load()

    def get(self, source: AgendaSource) -> SourcePause | None:
        return self._load().get(source)

    def lifted_at(self, source: AgendaSource) -> datetime | None:
        """When this source's pause last ran out, or `None`.

        The detectors need it. A pause does not archive, cancel or age the
        items that caused it: `kernel.pause_source` only stops the source being
        dispatched, so the same group is still inside `loop_window_hours` when
        the pause lifts, and a detector that counted the whole window would
        re-pause on the very tick that lifted it. What each detector is for is
        what the source has done SINCE, and this is where that line is.
        """
        self._load()
        lift = self._recent.get(source)
        return lift.lifted_at if lift is not None else None

    def add(
        self,
        source: AgendaSource,
        *,
        detector: str,
        reason: str,
        evidence: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> SourcePause | None:
        """Persist a new pause. Returns the pause record on first
        application, ``None`` if the source was already paused (idempotent)."""
        pauses = self._load()
        if source in pauses:
            return None
        at = now or datetime.now(timezone.utc)
        pause = self._with_expiry(
            source, detector, at, reason=reason, evidence=evidence or {},
        )
        pauses[source] = pause
        # The run is carried on the pause now, so the memory of the lift that
        # started it has been spent.
        self._recent.pop(source, None)
        self._persist()
        _append_log({
            "event": "pause",
            "ts": pause.paused_at.isoformat(),
            "source": source.value,
            "detector": detector,
            "reason": reason,
            "evidence": evidence or {},
            "expires_at": pause.expires_at.isoformat() if pause.expires_at else None,
            "consecutive": pause.consecutive,
        })
        self._fire_broadcast(
            "governor_pause_added",
            {
                "source": source.value,
                "detector": detector,
                "reason": reason,
                "paused_at": pause.paused_at.isoformat(),
                "expires_at": pause.expires_at.isoformat() if pause.expires_at else None,
                "consecutive": pause.consecutive,
            },
        )
        return pause

    def _with_expiry(
        self,
        source: AgendaSource,
        detector: str,
        at: datetime,
        *,
        reason: str,
        evidence: dict[str, Any],
    ) -> SourcePause:
        """This pause's clock, and where it sits in a run of them.

        A source paused again inside the wait it just served is one the lift
        did not fix, so the next wait doubles to the cap. One that behaved for
        longer than that starts over: the backoff is for a source that keeps
        coming back, not a record of everything it has ever done.
        """
        base = self._policy.base_for(detector)
        if base is None:
            return SourcePause(
                source=source, paused_at=at, detector=detector, reason=reason,
                evidence=evidence, chain_started_at=at,
            )
        lift = self._recent.get(source)
        seconds, consecutive, chain = base, 1, at
        if lift is not None and (at - lift.lifted_at).total_seconds() < lift.ttl_seconds:
            consecutive = lift.consecutive + 1
            chain = lift.chain_started_at
            seconds = min(
                lift.ttl_seconds * self._policy.backoff_multiplier,
                self._policy.max_ttl_seconds or lift.ttl_seconds,
            )
        return SourcePause(
            source=source,
            paused_at=at,
            detector=detector,
            reason=reason,
            evidence=evidence,
            expires_at=at + timedelta(seconds=seconds),
            consecutive=consecutive,
            chain_started_at=chain,
        )

    def expire_due(self, *, now: datetime | None = None) -> list[SourcePause]:
        """Lift every pause whose time is up, and remember that it happened.

        Returns what was lifted so the caller can reconcile its own cache: the
        kernel answers `is_source_paused` from memory between ticks, and a
        pause cleared only on disk would leave the source parked until the next
        backend boot.
        """
        at = now or datetime.now(timezone.utc)
        pauses = self._load()
        due = [
            pause for pause in pauses.values()
            if pause.expires_at is not None and pause.expires_at <= at
        ]
        for pause in due:
            pauses.pop(pause.source, None)
            self._recent[pause.source] = RecentLift(
                lifted_at=at,
                consecutive=pause.consecutive,
                ttl_seconds=max(
                    (pause.expires_at - pause.paused_at).total_seconds(), 0.0
                ) if pause.expires_at else 0.0,
                chain_started_at=pause.chain_started_at or pause.paused_at,
            )
        if not due:
            return []
        self._persist()
        for pause in due:
            _append_log({
                "event": "unpause",
                "ts": at.isoformat(),
                "source": pause.source.value,
                "by": BY_EXPIRY,
                "reason": REASON_EXPIRED,
                "paused_for_seconds": round(
                    (at - pause.paused_at).total_seconds(), 1
                ),
                "consecutive": pause.consecutive,
            })
            self._fire_broadcast(
                "governor_pause_removed",
                {
                    "source": pause.source.value,
                    "by": BY_EXPIRY,
                    "reason": REASON_EXPIRED,
                },
            )
        return due

    def remove(
        self,
        source: AgendaSource,
        *,
        by: str = "operator",
        reason: str = REASON_OPERATOR_UNPAUSE,
        now: datetime | None = None,
    ) -> SourcePause | None:
        """Clear a pause. Returns the cleared pause record, or ``None``
        if the source wasn't paused."""
        pauses = self._load()
        pause = pauses.pop(source, None)
        if pause is None:
            return None
        # A hand on the control is a decision that this source is fine, so the
        # run of pauses ends here. The backoff is for a source the runtime
        # keeps having to park by itself, and counting an operator's lift
        # against it would make their own judgement the thing that escalates.
        self._recent.pop(source, None)
        self._persist()
        _append_log({
            "event": "unpause",
            "ts": (now or datetime.now(timezone.utc)).isoformat(),
            "source": source.value,
            "by": by,
            "reason": reason,
        })
        self._fire_broadcast(
            "governor_pause_removed",
            {"source": source.value, "by": by, "reason": reason},
        )
        return pause


NotifyFn = Callable[[SourcePause], Awaitable[None]]


@dataclass
class GovernorTickResult:
    """One detector pass. Tests assert on it; the dashboard surfaces these
    alongside the kernel tick results."""

    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    pauses_added: list[SourcePause] = field(default_factory=list)
    workers_cancelled: list[str] = field(default_factory=list)
    items_blocked: list[str] = field(default_factory=list)
    #: Pauses this tick lifted because their time was up. Beside the ones it
    #: added, because a tick that only ever reported new pauses could not show
    #: the operator that anything ever comes back.
    pauses_expired: list[SourcePause] = field(default_factory=list)
    #: Sources whose run of pauses reached `advice_after`, as the agenda ids
    #: of the cards filed for them.
    advice_cards: list[str] = field(default_factory=list)


class Governor:
    """The stop-digging detector. Owns the ``PauseStore`` + the three
    detectors. The kernel reads pause state via the store; the Governor
    writes it. Outbound notification on every new pause is rate-cap-exempt
    — the operator MUST see when autonomy stops trusting a source."""

    def __init__(
        self,
        *,
        agenda_store: AgendaStore,
        pause_store: PauseStore,
        config: GovernorConfig | None = None,
        notify_fn: NotifyFn | None = None,
        kernel_pause_hook: Callable[[AgendaSource, str], None] | None = None,
        kernel_resume_hook: Callable[[AgendaSource], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._agenda = agenda_store
        self._pauses = pause_store
        self._config = config or GovernorConfig()
        self._notify = notify_fn
        self._kernel_pause_hook = kernel_pause_hook
        # Its opposite, and it has to exist. The kernel answers
        # `is_source_paused` from memory between ticks, so a pause lifted only
        # on disk leaves the source parked until the next backend boot, which
        # is the bug the REST route already works around by hand.
        self._kernel_resume_hook = kernel_resume_hook
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._loop_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        # ``_apply_pause`` fires ``_safe_notify`` as a background task so
        # the detector loop never blocks on a slow channel. Tracking the
        # set lets ``stop()`` drain in-flight notifications cleanly
        # instead of leaking orphaned tasks past shutdown.
        self._notify_tasks: set[asyncio.Task[None]] = set()
        # The dashboard reads this to render the most recent detector
        # pass. Updated unconditionally at the end of ``run_once`` even
        # on no-change ticks so the operator sees a fresh ``at`` stamp.
        self._last_tick_result: GovernorTickResult | None = None
        # Optional governor_tick broadcaster wired by
        # Mirror server boot so the operator's Autonomy tab refreshes on
        # every cadence without polling /api/governor/state.
        self._tick_broadcast_hook: Callable[[dict[str, Any]], None] | None = None

    def set_tick_broadcast_hook(
        self,
        hook: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        self._tick_broadcast_hook = hook

    @property
    def config(self) -> GovernorConfig:
        return self._config

    @property
    def is_running(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    @property
    def last_tick(self) -> "GovernorTickResult | None":
        return self._last_tick_result

    # -- Lifecycle ---------------------------------------------------

    async def start(self) -> None:
        if self.is_running:
            return
        self._stopping.clear()
        self._loop_task = asyncio.create_task(self._run_loop(), name="autonomy-governor")
        log.info(
            "governor: started (cadence=%.0fs loop_n=%d/%dh cost_x=%.1f trust_n=%d)",
            self._config.cadence_seconds,
            self._config.loop_n,
            self._config.loop_window_hours,
            self._config.cost_threshold_multiplier,
            self._config.trust_consecutive_rejections,
        )

    NOTIFY_DRAIN_BUDGET_SECONDS = 5.0

    async def stop(self) -> None:
        if not self.is_running:
            return
        self._stopping.set()
        task = self._loop_task
        self._loop_task = None
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=self.NOTIFY_DRAIN_BUDGET_SECONDS)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        # Drain any in-flight notify tasks against a small shared budget.
        if self._notify_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._notify_tasks, return_exceptions=True),
                    timeout=self.NOTIFY_DRAIN_BUDGET_SECONDS,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "governor: %d in-flight notify task(s) did not drain "
                    "within %.0fs shutdown budget",
                    len(self._notify_tasks),
                    self.NOTIFY_DRAIN_BUDGET_SECONDS,
                )
        log.info("governor: stopped")

    async def _run_loop(self) -> None:
        try:
            while not self._stopping.is_set():
                try:
                    await self.run_once()
                except Exception:
                    log.exception("governor: detector cycle raised — continuing")
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(),
                        timeout=self._config.cadence_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            return

    # -- Detector driver --------------------------------------------

    async def run_once(self) -> GovernorTickResult:
        """One detector pass. Tests call this synchronously; the loop
        calls it on cadence. Returns the new pauses + side-effects so the
        operator-facing dashboard can stream the activity."""
        result = GovernorTickResult(at=self._clock())
        # First, because a pause that has served its time must not stop this
        # tick's detectors from looking at the source again. Nothing here
        # spends or blocks: it is a file read and, on the rare tick that has
        # one to lift, a write.
        for lifted in self._lift_expired(result.at):
            result.pauses_expired.append(lifted)
            if self._kernel_resume_hook is not None:
                try:
                    self._kernel_resume_hook(lifted.source)
                except Exception:
                    log.exception(
                        "governor: kernel_resume_hook raised for %s", lifted.source
                    )
        # Both collectors read and parse every matching agenda file, which
        # is CPU and blocking I/O on a cadence — on the loop it showed up as
        # multi-second lag spikes while the operator was idle. Neither
        # mutates anything, so a thread is the whole fix.
        items, archive_items = await asyncio.gather(
            asyncio.to_thread(self._collect_items_in_window),
            asyncio.to_thread(self._collect_archive_items_in_window),
        )
        all_items = items + archive_items

        # Loop detector — must see archive items too: the AgendaStore
        # dedupes new admissions against ACTIVE items only, so a goal
        # the operator keeps cancelling cycles active → archive each
        # round and would otherwise be invisible to a detector that
        # only inspects ``active/``.
        for pause in self._detect_loops(all_items):
            self._apply_pause(pause, result)

        # Cost spiral detector — operates on live workers + their items.
        for worker_id, item_id in self._detect_cost_spirals(items):
            self._cancel_worker_and_block_item(worker_id, item_id, result)

        # Trust degradation — walks active + archive.
        for pause in self._detect_trust_degradation(all_items):
            self._apply_pause(pause, result)

        self._last_tick_result = result
        if self._tick_broadcast_hook is not None:
            try:
                self._tick_broadcast_hook(
                    {
                        "at": result.at.isoformat(),
                        "pauses_added": [p.source.value for p in result.pauses_added],
                        "workers_cancelled": list(result.workers_cancelled),
                    }
                )
            except Exception:
                log.exception("governor tick broadcast hook raised; non-fatal")
        return result

    # -- Loop detector ----------------------------------------------

    def _after_last_lift(self, items: list[AgendaItem]) -> list[AgendaItem]:
        """Only what a source has done since its own pause last ran out.

        **A pause does not clear the evidence that caused it.**
        `kernel.pause_source` stops the source being dispatched and leaves its
        items active, so the group that tripped the detector is still whole
        inside `loop_window_hours` when the pause expires. Without this filter
        the tick that LIFTS a pause re-pauses on the very same pass, off items
        the source produced before it was ever stopped: `run_once` lifts first
        so the source can be looked at again, and `_detect_loops` then looked
        at the same six-hour-old evidence. The backoff read that as the source
        coming straight back and climbed to the advice card while the source
        had done nothing at all.

        A source with no recorded lift is unfiltered, which is every source
        that has never been paused and every one the OPERATOR lifted: their
        hand on the control ends the run, so the next count starts clean.
        """
        out: list[AgendaItem] = []
        for item in items:
            lift = self._pauses.lifted_at(item.source)
            if lift is not None and _stamp(item) <= lift:
                continue
            out.append(item)
        return out

    def _detect_loops(self, items: list[AgendaItem]) -> list[SourcePause]:
        """Group active items by ``(source, dedupe_key)``; any group at
        or above ``loop_n`` triggers a pause for that source."""
        n = self._config.loop_n
        if n <= 1:
            return []
        groups: dict[tuple[AgendaSource, str], list[str]] = {}
        for item in self._after_last_lift(items):
            key = (item.source, dedupe_key(item.goal, item.source))
            groups.setdefault(key, []).append(item.id)

        pauses: list[SourcePause] = []
        for (source, key), ids in groups.items():
            if len(ids) < n:
                continue
            if self._pauses.is_paused(source):
                continue
            pause = SourcePause(
                source=source,
                paused_at=self._clock(),
                detector=DETECTOR_LOOP,
                reason=REASON_LOOP_DETECTED,
                evidence={
                    "dedupe_key": key,
                    "count": len(ids),
                    "window_hours": self._config.loop_window_hours,
                    "agenda_ids": ids[:10],
                },
            )
            pauses.append(pause)
        return pauses

    # -- Cost spiral detector ---------------------------------------

    def _detect_cost_spirals(
        self,
        items: list[AgendaItem],
    ) -> list[tuple[str, str]]:
        """Walk every active worker; compare its ``tokens_in + tokens_out``
        against the linked item's ``budget_tokens_cap`` and its
        ``duration_seconds`` against ``budget_seconds_cap``. Returns
        ``(worker_id, item_id)`` pairs that crossed the threshold."""
        threshold = self._config.cost_threshold_multiplier
        if threshold <= 0:
            return []
        item_by_id = {item.id: item for item in items}
        offenders: list[tuple[str, str]] = []
        try:
            workers = list_active_records()
        except Exception:
            log.exception("governor: list_active_records failed")
            return []
        for record in workers:
            if record.status in WORKER_TERMINAL_STATUSES:
                continue
            item = item_by_id.get(record.agenda_item_id)
            if item is None:
                continue
            if self._worker_over_budget(record, item, threshold):
                offenders.append((record.id, item.id))
        return offenders

    @staticmethod
    def _worker_over_budget(
        record: WorkerRecord,
        item: AgendaItem,
        threshold: float,
    ) -> bool:
        token_cap = item.budget_tokens_cap
        if token_cap > 0:
            spent = int(record.tokens_in or 0) + int(record.tokens_out or 0)
            if spent >= int(token_cap * threshold):
                return True
        seconds_cap = item.budget_seconds_cap
        if seconds_cap > 0:
            duration = float(record.duration_seconds or 0.0)
            if duration >= float(seconds_cap) * threshold:
                return True
        return False

    def _cancel_worker_and_block_item(
        self,
        worker_id: str,
        item_id: str,
        result: GovernorTickResult,
    ) -> None:
        try:
            from tesseract.orchestrator.workers.record import load_record

            record = load_record(worker_id)
            if record is not None and record.status not in WORKER_TERMINAL_STATUSES:
                record.transition_to(
                    WorkerStatus.CANCELLED,
                    reason="governor_cost_spiral",
                )
                write_record(record)
                result.workers_cancelled.append(worker_id)
                _append_log({
                    "event": "cost_spiral_cancel",
                    "ts": self._clock().isoformat(),
                    "worker_id": worker_id,
                    "item_id": item_id,
                })
                operator_journal.append(
                    "outcome",
                    {
                        "agenda_item_id": record.agenda_item_id or item_id,
                        "worker_id": record.id,
                        "status": record.status.value,
                        "summary": "governor_cost_spiral",
                        "artifacts": len(record.artifacts or []),
                    },
                )
        except Exception:
            log.exception("governor: failed to cancel worker %s", worker_id)

        try:
            item = self._agenda.get(item_id)
            if item is not None and not item.is_terminal():
                item.blocked_reason = REASON_COST_SPIRAL
                self._agenda.transition(
                    item,
                    AgendaStatus.BLOCKED,
                    reason=REASON_COST_SPIRAL,
                    by="governor",
                )
                result.items_blocked.append(item_id)
        except Exception:
            log.exception("governor: failed to block agenda item %s", item_id)

    # -- Trust degradation detector ---------------------------------

    def _detect_trust_degradation(
        self,
        items: list[AgendaItem],
    ) -> list[SourcePause]:
        """For each source, look at the most recent N items that passed
        through ``awaiting_operator``. If every one terminated in
        ``cancelled`` / ``abandoned`` (operator rejection), pause."""
        n = self._config.trust_consecutive_rejections
        if n <= 0:
            return []
        by_source: dict[AgendaSource, list[AgendaItem]] = {}
        # Since the lift, for the reason `_after_last_lift` gives: the
        # rejections that paused a source are still in the window when its
        # pause expires. This detector's ttl equals its window today, so the
        # overlap is a boundary rather than a certainty, but one rule for both
        # detectors is worth more than a second reading of the same hazard.
        for item in self._after_last_lift(items):
            if not _passed_through_awaiting_operator(item):
                continue
            by_source.setdefault(item.source, []).append(item)

        pauses: list[SourcePause] = []
        for source, group in by_source.items():
            if self._pauses.is_paused(source):
                continue
            # Newest first; the kernel transition history sets updated_at
            # on every save, so updated_at is the cleanest sort key.
            group.sort(key=lambda i: i.updated_at, reverse=True)
            recent = group[:n]
            if len(recent) < n:
                continue
            if not all(_is_operator_rejection(i) for i in recent):
                continue
            pauses.append(SourcePause(
                source=source,
                paused_at=self._clock(),
                detector=DETECTOR_TRUST_DEGRADATION,
                reason=REASON_TRUST_DEGRADED,
                evidence={
                    "consecutive_rejections": n,
                    "agenda_ids": [i.id for i in recent],
                },
            ))
        return pauses

    # -- Pause application ------------------------------------------

    def _apply_pause(
        self,
        pause: SourcePause,
        result: GovernorTickResult,
    ) -> None:
        applied = self._pauses.add(
            pause.source,
            detector=pause.detector,
            reason=pause.reason,
            evidence=pause.evidence,
            now=pause.paused_at,
        )
        if applied is None:
            return
        result.pauses_added.append(applied)
        card = self._ask_if_it_keeps_coming_back(applied)
        if card:
            result.advice_cards.append(card)
        if self._kernel_pause_hook is not None:
            try:
                self._kernel_pause_hook(applied.source, applied.reason)
            except Exception:
                log.exception("governor: kernel_pause_hook raised for %s", applied.source)
        if self._notify is not None:
            notify_task = asyncio.create_task(
                self._safe_notify(applied),
                name=f"governor-notify:{applied.source.value}",
            )
            self._notify_tasks.add(notify_task)
            notify_task.add_done_callback(self._notify_tasks.discard)

    def _lift_expired(self, now: datetime) -> list[SourcePause]:
        """Every pause whose time has run out, cleared. Never raises: a store
        that cannot be read must not stop the detectors from running."""
        try:
            return self._pauses.expire_due(now=now)
        except Exception:  # noqa: BLE001
            log.exception("governor: could not lift expired pauses")
            return []

    def _ask_if_it_keeps_coming_back(self, pause: SourcePause) -> str:
        """File the card when a source has been parked `advice_after` times in
        a row, and return its id.

        The backoff alone would keep doubling in silence, which is a capability
        going off for three days with nobody told. The count is what turns a
        run of pauses into a decision, and the decision is the operator's:
        whether this source is worth having at all is not a judgement the
        runtime makes about itself.

        Never raises. The pause is already written when this runs, so a
        paperwork failure loses the card and not the safeguard.
        """
        after = self._pauses.policy.advice_after
        if after < 1 or pause.consecutive < after:
            return ""
        try:
            from tesseract.orchestrator.healing import stop_rule

            since = pause.chain_started_at or pause.paused_at
            stop = stop_rule.advice_owed(
                kind="source_paused",
                subject=pause.source.value,
                said=(
                    f"{pause.source.value} has been paused {pause.consecutive} "
                    f"times in a row since "
                    f"{since.isoformat(timespec='minutes')}, most recently for "
                    f"{pause.reason or pause.detector}. Each pause lifted "
                    f"itself and the source came straight back, so waiting "
                    f"longer is not going to settle it. What is worth deciding "
                    f"is whether this source should keep proposing work"
                ),
            )
            item = stop_rule.file_card(stop, since=since, now=pause.paused_at)
            return getattr(item, "id", "")
        except Exception:  # noqa: BLE001
            log.exception(
                "governor: could not file the advice card for %s", pause.source.value
            )
            return ""

    async def _safe_notify(self, pause: SourcePause) -> None:
        try:
            await self._notify(pause)
        except Exception:
            log.exception("governor: notify_fn raised for %s", pause.source.value)

    # -- Helpers ----------------------------------------------------

    def _collect_items_in_window(self) -> list[AgendaItem]:
        cutoff = self._clock() - timedelta(hours=self._config.loop_window_hours)
        return [item for item in self._agenda.iter_active() if _in_window(item, cutoff)]

    def _collect_archive_items_in_window(self) -> list[AgendaItem]:
        """Walk archive/*/ within the loop window. The trust detector needs
        terminal items to count rejections, and the loop detector needs them
        to count repeats across cancel cycles.

        Bucket names are the `%Y-%m` of `updated_at` (`agenda_store.py`
        archives with exactly that), so a bucket older than the month the
        window opens in cannot hold an item inside the window — skipped
        without opening it. The docstring here has always claimed "at most
        two month dirs touch the window"; the code parsed and validated
        every item ever archived and filtered afterwards, so the cost grew
        without bound as the archive did.
        """
        cutoff = self._clock() - timedelta(hours=self._config.loop_window_hours)
        earliest_bucket = cutoff.astimezone(timezone.utc).strftime("%Y-%m")
        root = agenda_archive_dir()
        if not root.exists():
            return []
        out: list[AgendaItem] = []
        for month_dir in sorted(root.iterdir()):
            if not month_dir.is_dir():
                continue
            # String compare is a date compare for zero-padded `%Y-%m`.
            if month_dir.name < earliest_bucket:
                continue
            for child in sorted(month_dir.iterdir()):
                if child.suffix != ".json":
                    continue
                try:
                    raw = json.loads(child.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                try:
                    item = AgendaItem.model_validate(raw)
                except Exception:
                    continue
                if _in_window(item, cutoff):
                    out.append(item)
        return out


def _stamp(item: AgendaItem) -> datetime:
    """When this item last moved, in UTC. One reading, because the window
    filter and the since-the-lift filter must agree about what an item's
    moment IS."""
    ts = item.updated_at if item.updated_at else item.created_at
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _in_window(item: AgendaItem, cutoff: datetime) -> bool:
    return _stamp(item) >= cutoff


def _passed_through_awaiting_operator(item: AgendaItem) -> bool:
    for tr in item.status_history:
        if tr.to_status == AgendaStatus.AWAITING_OPERATOR:
            return True
    return False


def _is_operator_rejection(item: AgendaItem) -> bool:
    """An ``awaiting_operator`` item terminated by operator cancellation
    or abandonment is a rejection. ``done`` (operator approved + finished)
    or ``superseded`` (something else replaced it) don't count."""
    if item.status not in {AgendaStatus.CANCELLED, AgendaStatus.ABANDONED}:
        return False
    return _passed_through_awaiting_operator(item)


__all__ = [
    "DETECTOR_COST_SPIRAL",
    "DETECTOR_LOOP",
    "DETECTOR_TRUST_DEGRADATION",
    "DEFAULT_CADENCE_SECONDS",
    "DEFAULT_COST_MULTIPLIER",
    "DEFAULT_LOOP_N",
    "DEFAULT_LOOP_WINDOW_HOURS",
    "DEFAULT_TRUST_CONSECUTIVE",
    "Governor",
    "GovernorConfig",
    "GovernorTickResult",
    "NotifyFn",
    "PauseStore",
    "REASON_COST_SPIRAL",
    "REASON_LOOP_DETECTED",
    "REASON_OPERATOR_UNPAUSE",
    "REASON_TRUST_DEGRADED",
    "SourcePause",
]
