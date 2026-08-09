"""Governor — the stop-digging detector layer (AU-6).

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

Unpause is operator-only via REST (AU-6 §7); the governor never
auto-clears a pause.
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

DETECTOR_LOOP = "loop"
DETECTOR_COST_SPIRAL = "cost_spiral"
DETECTOR_TRUST_DEGRADATION = "trust_degradation"


@dataclass(frozen=True)
class GovernorConfig:
    cadence_seconds: float = DEFAULT_CADENCE_SECONDS
    loop_n: int = DEFAULT_LOOP_N
    loop_window_hours: int = DEFAULT_LOOP_WINDOW_HOURS
    cost_threshold_multiplier: float = DEFAULT_COST_MULTIPLIER
    trust_consecutive_rejections: int = DEFAULT_TRUST_CONSECUTIVE

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
        )


@dataclass
class SourcePause:
    """One source's pause state. Serialised to ``source-pauses.json``."""

    source: AgendaSource
    paused_at: datetime
    detector: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "paused_at": self.paused_at.isoformat(),
            "detector": self.detector,
            "reason": self.reason,
            "evidence": self.evidence,
        }

    @classmethod
    def from_payload(cls, raw: dict[str, Any]) -> "SourcePause | None":
        try:
            source = AgendaSource(raw["source"])
        except (KeyError, ValueError):
            return None
        try:
            paused_at = datetime.fromisoformat(str(raw["paused_at"]))
        except (KeyError, ValueError):
            return None
        if paused_at.tzinfo is None:
            paused_at = paused_at.replace(tzinfo=timezone.utc)
        return cls(
            source=source,
            paused_at=paused_at,
            detector=str(raw.get("detector", "")),
            reason=str(raw.get("reason", "")),
            evidence=raw.get("evidence") or {},
        )


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

    Phase 4 (2026-05-22): optional ``broadcast_hook`` fan-outs
    ``governor_pause_added`` / ``governor_pause_removed`` envelopes so the
    Mirror Autonomy tab refreshes immediately. Mirror server boot wires
    the hook; REPL / standalone contexts leave it unset.
    """

    def __init__(
        self,
        *,
        broadcast_hook: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._cache: dict[AgendaSource, SourcePause] | None = None
        self._broadcast_hook = broadcast_hook

    def set_broadcast_hook(
        self,
        hook: Callable[[str, dict[str, Any]], None] | None,
    ) -> None:
        self._broadcast_hook = hook

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
        self._cache = out
        return out

    def _persist(self) -> None:
        pauses = self._load()
        payload = {"pauses": [p.to_payload() for p in pauses.values()]}
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
        pause = SourcePause(
            source=source,
            paused_at=(now or datetime.now(timezone.utc)),
            detector=detector,
            reason=reason,
            evidence=evidence or {},
        )
        pauses[source] = pause
        self._persist()
        _append_log({
            "event": "pause",
            "ts": pause.paused_at.isoformat(),
            "source": source.value,
            "detector": detector,
            "reason": reason,
            "evidence": evidence or {},
        })
        self._fire_broadcast(
            "governor_pause_added",
            {
                "source": source.value,
                "detector": detector,
                "reason": reason,
                "paused_at": pause.paused_at.isoformat(),
            },
        )
        return pause

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
    """One detector pass. Tests assert on it; the dashboard will surface
    these in AU-7 alongside the kernel tick results."""

    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    pauses_added: list[SourcePause] = field(default_factory=list)
    workers_cancelled: list[str] = field(default_factory=list)
    items_blocked: list[str] = field(default_factory=list)


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
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._agenda = agenda_store
        self._pauses = pause_store
        self._config = config or GovernorConfig()
        self._notify = notify_fn
        self._kernel_pause_hook = kernel_pause_hook
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._loop_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        # ``_apply_pause`` fires ``_safe_notify`` as a background task so
        # the detector loop never blocks on a slow channel. Tracking the
        # set lets ``stop()`` drain in-flight notifications cleanly
        # instead of leaking orphaned tasks past shutdown.
        self._notify_tasks: set[asyncio.Task[None]] = set()
        # AU-7 dashboard reads this to render the most recent detector
        # pass. Updated unconditionally at the end of ``run_once`` even
        # on no-change ticks so the operator sees a fresh ``at`` stamp.
        self._last_tick_result: GovernorTickResult | None = None
        # Phase 4 (2026-05-22) — optional governor_tick broadcaster wired by
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
        operator-facing dashboard (AU-7) can stream the activity."""
        result = GovernorTickResult(at=self._clock())
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

    def _detect_loops(self, items: list[AgendaItem]) -> list[SourcePause]:
        """Group active items by ``(source, dedupe_key)``; any group at
        or above ``loop_n`` triggers a pause for that source."""
        n = self._config.loop_n
        if n <= 1:
            return []
        groups: dict[tuple[AgendaSource, str], list[str]] = {}
        for item in items:
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
        for item in items:
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


def _in_window(item: AgendaItem, cutoff: datetime) -> bool:
    ts = item.updated_at if item.updated_at else item.created_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts >= cutoff


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
