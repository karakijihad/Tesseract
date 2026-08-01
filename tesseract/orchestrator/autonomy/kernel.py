"""AutonomyKernel — long-lived asyncio service.

The persistent executive function. NOT a chat loop. Each ``tick``
follows the deterministic 9-step flow in
``_shared/autonomy-kernel-protocol.md §Per-tick flow``:

1. Drain event bus (since last tick).
2. Convert events → ``AgendaItemDraft`` via the registered mappers.
3. Persist new candidates as ``AgendaItem(status=proposed)``; dedupe
   by ``(source, source_event_id)`` and ``(source, goal)``.
4. Recompute scores (the store does this on every save).
5. Governor check: items with paused source stay parked.
6. Selection: top-K by ``priority_score`` where lane has headroom and
   risk class admits.
7. (S2) For each selected item: generate rationale + dispatch worker.
   S1 stops here: transitions selected items to ``SELECTED`` so the
   dashboard renders the selection without spinning up a worker.
8. (S2) Outbound notify.
9. Sleep until: tick interval elapsed OR explicit ``poke`` from a
   publisher.

S1 ships steps 1-6 + the lifecycle scaffolding (start / stop /
quiesce / resume). S2 wires rationale generation, worker dispatch,
and operator notify.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from tesseract.orchestrator.autonomy.agenda_store import (
    AgendaStore,
    load_weights_from_yaml,
)
from tesseract.orchestrator.autonomy.drafts import AgendaItemDraft
from tesseract.orchestrator.autonomy.event_bus import (
    AutonomyEvent,
    AutonomyEventBus,
)
from tesseract.orchestrator.autonomy.follow_up_mapper import (
    FollowUpConfig,
    FollowUpMapper,
)
from tesseract.orchestrator.autonomy.governor import PauseStore
from tesseract.orchestrator.autonomy.mappers import DEFAULT_MAPPERS
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    RiskClass,
)
from tesseract.orchestrator.autonomy.prune_ledger import (
    PruneRecord,
    PruneStage,
    record_prune,
)
from tesseract.orchestrator.autonomy.rationale import (
    RationaleAdapter,
    UNAVAILABLE_MARKER,
    generate_rationale,
)
from tesseract.orchestrator.autonomy.text_quality import is_degenerate_goal
from tesseract.orchestrator.autonomy.worker_dispatch import (
    WorkerRunner,
    build_worker_record,
    default_runner,
)
from tesseract.orchestrator.autonomy import journal as operator_journal
from tesseract.orchestrator.workers.lane import (
    AdmissionDecision,
    AdmissionResult,
    WorkerLane,
)
from tesseract.orchestrator.workers.record import (
    TERMINAL_STATUSES as WORKER_TERMINAL_STATUSES,
    WorkerStatus,
    iter_active_status_summary,
    write_record,
)
from tesseract.orchestrator.workers.worktree import (
    WorktreeError,
    allocate_for_record as _allocate_worktree_for_record,
    finalize_for_record as _finalize_worktree_for_record,
)
from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.activity.hooks import (
    fail_autonomy,
    register_autonomy,
    remove_autonomy,
)

log = logging.getLogger(__name__)


DEFAULT_TICK_SECONDS = 30.0
DEFAULT_TOP_K = 3
DEFAULT_MAX_CONCURRENT_TOTAL = 8
DEFAULT_MAX_OPEN_TOTAL = 40
DEFAULT_MAX_OPEN_PER_SOURCE = 8
DEFAULT_FUZZY_THRESHOLD = 0.9
DEFAULT_FUZZY_WINDOW_HOURS = 24
DEFAULT_MAX_UNVETTED_HOURS = 24
DEFAULT_MAX_RESUME_ATTEMPTS = 2
DEFAULT_VET_REQUIRED: tuple[str, ...] = (
    "self_reflection",
    "memory_signal",
    "vault_signal",
)

# Risk class → worker kind. Used in S1 to pick a lane for an
# admission *check* (selection only); S2 swaps in real per-item
# kind selection driven by the mapper.
_DEFAULT_KIND_FOR_RISK: dict[RiskClass, WorkerKind] = {
    RiskClass.AUTONOMOUS: WorkerKind.TARS_SELF,
    RiskClass.PROPOSE: WorkerKind.MARKDOWN_AGENT,
    RiskClass.OPERATOR_GATE: WorkerKind.CLAUDE_CLI,
}


def _kind_for_item(item: AgendaItem) -> WorkerKind | None:
    """Resolve an agenda item to a concrete WorkerKind.

    Dynamic routing for ``OPERATOR_GATE`` items: if the TARS controller
    daemon's port file is on disk AND a TCP probe succeeds, the
    dispatcher routes through the controller (whose chat brain
    orchestrates claude/codex/agents) instead of spawning a fresh
    ``claude_cli`` advisor. The static ``_DEFAULT_KIND_FOR_RISK`` map
    remains the fallback so a missing controller does not block
    dispatch — the operator-gate item still runs, just through the
    historical CLI path.

    History:

    * TC-4 (2026-05-23) introduced the dynamic branch.
    * Audit-2 C-1 (2026-05-23) reverted it because the runner couldn't
      dispatch ``TARS_CONTROLLER`` — every selection hit
      ``unsupported_kind``.
    * 2026-05-24 — re-enabled now that ``KernelWorkerRunner._route_for_kind``
      routes ``TARS_CONTROLLER`` through the ``delegate_tars_controller``
      tool, which uses the shared dispatcher to mint a session and
      tail the controller's reply.
    """
    base = _DEFAULT_KIND_FOR_RISK.get(item.risk_class)
    if base is WorkerKind.CLAUDE_CLI:
        try:
            from tesseract.orchestrator.tars_controller import (
                controller_port_alive,
            )
        except Exception:  # noqa: BLE001 — never let the import gate dispatch
            return base
        try:
            if controller_port_alive():
                return WorkerKind.TARS_CONTROLLER
        except Exception:  # noqa: BLE001
            log.debug("kernel: controller_port_alive probe raised", exc_info=True)
    return base


# Canonical ``last_decision`` reasons. Tests import these constants so
# string drift doesn't silently break assertions — same pattern as
# ``workers/lane.py::REASON_*``.
REASON_SELECTED = "selected_by_tick"
REASON_LANE_FULL = "lane_full"
REASON_RISK_MISMATCH = "risk_mismatch"
REASON_LANE_UNCONFIGURED = "lane_unconfigured"
REASON_GOVERNOR_PAUSED = "governor_paused_source"
REASON_DAILY_CAP_PAUSE = "daily_cap_reached"
REASON_TOTAL_CONCURRENCY_BLOCK = "max_concurrent_workers_total_reached"
REASON_DEDUPE_HIT = "dedupe_hit"
REASON_AWAITING_OPERATOR = "awaiting_operator_approval"
REASON_RESUME_ALL_DONE = "resume_all_done"
REASON_RESUME_RETRY = "resume_retry"
REASON_RESUME_EXHAUSTED = "resume_attempts_exhausted"


@dataclass(frozen=True)
class KernelConfig:
    """Deterministic selection config loaded from ``agenda.yaml``.

    ``daily_usd_cap`` (from ``daily_caps.usd``) gates autonomous dispatch on
    the cost ledger's **global** daily USD total (all spend today — chat,
    observer, voice, autonomy), not an autonomy-only subtotal. The kernel
    reads that total via an injected ``daily_usd_spent`` accessor
    (``CostLedger.snapshot()['global']['spent_usd']``); when no accessor is
    wired (test fixtures) the USD cap is skipped, never silently faked.
    ``AgendaStore.today_spend()`` still supplies the tokens/seconds totals."""

    tick_interval_seconds: float = DEFAULT_TICK_SECONDS
    top_k: int = DEFAULT_TOP_K
    max_concurrent_workers_total: int = DEFAULT_MAX_CONCURRENT_TOTAL
    daily_tokens_cap: int = 0
    daily_seconds_cap: int = 0
    daily_usd_cap: float = 0.0
    exceed_behavior: str = "pause"
    max_open_total: int = DEFAULT_MAX_OPEN_TOTAL
    max_open_per_source: int = DEFAULT_MAX_OPEN_PER_SOURCE
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD
    fuzzy_window_hours: int = DEFAULT_FUZZY_WINDOW_HOURS
    vet_enabled: bool = False
    vet_required: frozenset[AgendaSource] = field(default_factory=frozenset)
    max_unvetted_hours: int = DEFAULT_MAX_UNVETTED_HOURS
    max_resume_attempts: int = DEFAULT_MAX_RESUME_ATTEMPTS

    @classmethod
    def from_yaml_dict(cls, raw: dict[str, Any]) -> "KernelConfig":
        kernel_block = raw.get("kernel") or {}
        caps = raw.get("daily_caps") or {}
        admission = raw.get("admission") or {}
        fuzzy = admission.get("fuzzy_dedupe") or {}
        vetter = raw.get("vetter") or {}
        vet_required: set[AgendaSource] = set()
        for name in vetter.get("vet_required", DEFAULT_VET_REQUIRED):
            try:
                vet_required.add(AgendaSource(name))
            except ValueError:
                log.warning("autonomy: vetter.vet_required unknown source %r", name)
        return cls(
            tick_interval_seconds=float(
                kernel_block.get("tick_interval_seconds", DEFAULT_TICK_SECONDS)
            ),
            top_k=int(kernel_block.get("top_k", DEFAULT_TOP_K)),
            max_concurrent_workers_total=int(
                kernel_block.get(
                    "max_concurrent_workers_total", DEFAULT_MAX_CONCURRENT_TOTAL
                )
            ),
            daily_tokens_cap=int(caps.get("tokens", 0)),
            daily_seconds_cap=int(caps.get("seconds", 0)),
            daily_usd_cap=float(caps.get("usd", 0.0)),
            exceed_behavior=str(caps.get("exceed_behavior", "pause")),
            max_open_total=int(admission.get("max_open_total", DEFAULT_MAX_OPEN_TOTAL)),
            max_open_per_source=int(
                admission.get("max_open_per_source", DEFAULT_MAX_OPEN_PER_SOURCE)
            ),
            fuzzy_threshold=float(fuzzy.get("threshold", DEFAULT_FUZZY_THRESHOLD)),
            fuzzy_window_hours=int(
                fuzzy.get("window_hours", DEFAULT_FUZZY_WINDOW_HOURS)
            ),
            vet_enabled=bool(vetter.get("enabled", False)),
            vet_required=frozenset(vet_required),
            max_unvetted_hours=int(
                vetter.get("max_unvetted_hours", DEFAULT_MAX_UNVETTED_HOURS)
            ),
            max_resume_attempts=int(
                kernel_block.get("max_resume_attempts", DEFAULT_MAX_RESUME_ATTEMPTS)
            ),
        )


@dataclass(frozen=True)
class MapperConfig:
    enabled: bool
    source: AgendaSource
    default_risk_class: RiskClass
    dedupe_window_hours: int

    @classmethod
    def from_yaml(cls, raw: dict[str, Any]) -> "MapperConfig | None":
        try:
            source = AgendaSource(raw.get("source"))
        except ValueError:
            return None
        try:
            risk = RiskClass(raw.get("default_risk_class", "propose"))
        except ValueError:
            risk = RiskClass.PROPOSE
        return cls(
            enabled=bool(raw.get("enabled", False)),
            source=source,
            default_risk_class=risk,
            dedupe_window_hours=int(raw.get("dedupe_window_hours", 24)),
        )


def load_mapper_configs(path: Path) -> dict[AgendaSource, MapperConfig]:
    """Parse ``agenda-mappers.yaml`` into a per-source config map."""
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[AgendaSource, MapperConfig] = {}
    for entry in (raw.get("mappers") or {}).values():
        if not isinstance(entry, dict):
            continue
        cfg = MapperConfig.from_yaml(entry)
        if cfg is not None:
            out[cfg.source] = cfg
    return out


@dataclass
class KernelTickResult:
    """Operator-facing record of one tick's decisions. The dashboard
    will surface this in AU-7; tests assert on it."""

    events_drained: int = 0
    drafts_emitted: int = 0
    items_created: int = 0
    items_deduped: int = 0
    selected: list[str] = field(default_factory=list)
    rejections: list[dict[str, Any]] = field(default_factory=list)
    paused: bool = False
    pause_reason: str | None = None
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class AutonomyKernel:
    """Long-lived deterministic orchestrator. Owns one event bus, one
    agenda store, one worker lane; runs a tick loop in an asyncio task.

    Construction is cheap; ``start`` launches the loop, ``stop`` cancels
    it. The kernel does no work in ``__init__`` so test fixtures build
    one and call ``tick`` synchronously without spinning the loop.
    """

    def __init__(
        self,
        *,
        agenda_store: AgendaStore,
        worker_lane: WorkerLane,
        config: KernelConfig | None = None,
        mappers: dict[AgendaSource, Any] | None = None,
        mapper_configs: dict[AgendaSource, MapperConfig] | None = None,
        event_bus: AutonomyEventBus | None = None,
        clock: Any | None = None,
        worker_runner: WorkerRunner | None = None,
        rationale_adapter: RationaleAdapter | None = None,
        rationale_role: str = "agents_default",
        pause_store: PauseStore | None = None,
        follow_up_mapper: FollowUpMapper | None = None,
        daily_usd_spent: Callable[[], float] | None = None,
    ) -> None:
        self._agenda = agenda_store
        # F6 — global daily USD accessor (CostLedger.snapshot global spent).
        # None in test fixtures / when no ledger is wired → USD cap skipped.
        self._daily_usd_spent = daily_usd_spent
        self._lane = worker_lane
        self._config = config or KernelConfig()
        self._mappers = mappers or DEFAULT_MAPPERS
        self._mapper_configs = mapper_configs or {}
        self._bus = event_bus or AutonomyEventBus()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._runner: WorkerRunner = worker_runner or default_runner()
        self._rationale_adapter = rationale_adapter
        self._rationale_role = rationale_role
        self._pause_store = pause_store
        # TC-7 — wire the follow-up mapper. Defaults to one over the same
        # agenda_store with default config; production wiring overrides
        # the config via ``agenda.yaml::follow_up_mapper``.
        self._follow_up_mapper: FollowUpMapper = (
            follow_up_mapper
            if follow_up_mapper is not None
            else FollowUpMapper(agenda_store)
        )

        # In-memory cache. ``pause_store`` is the durable source of truth;
        # this set lets ``_is_source_paused`` answer on the hot path without
        # touching disk every check. The cache is repopulated at init and
        # mutated by ``pause_source`` / ``resume_source``.
        self._paused_sources: set[AgendaSource] = (
            set(self._pause_store.reload().keys()) if self._pause_store is not None else set()
        )
        self._quiesced = False
        self._loop_task: asyncio.Task[None] | None = None
        self._poke = asyncio.Event()
        self._stopping = asyncio.Event()
        # When True the kernel still drains events into the store but
        # stops admitting new selections (e.g. daily cap reached).
        self._dispatch_paused = False
        self._dispatch_pause_reason: str | None = None
        # Background tasks driving each in-flight worker. Kernel.stop()
        # awaits these to bound shutdown drain time.
        self._dispatch_tasks: set[asyncio.Task[None]] = set()

    # -- Surface ------------------------------------------------------

    @property
    def bus(self) -> AutonomyEventBus:
        return self._bus

    @property
    def config(self) -> KernelConfig:
        return self._config

    @property
    def is_running(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    @property
    def is_quiesced(self) -> bool:
        return self._quiesced

    @property
    def dispatch_paused(self) -> bool:
        return self._dispatch_paused

    @property
    def dispatch_pause_reason(self) -> str | None:
        return self._dispatch_pause_reason

    def pause_source(
        self,
        source: AgendaSource,
        *,
        reason: str = "",
        detector: str = "kernel",
    ) -> None:
        """Park ``source`` until ``resume_source`` is called. AU-6: when
        a ``pause_store`` is injected the pause is also persisted so it
        survives backend restart."""
        self._paused_sources.add(source)
        if self._pause_store is not None:
            self._pause_store.add(source, detector=detector, reason=reason)
        log.info("autonomy: source %s paused (%s)", source.value, reason)

    def resume_source(
        self,
        source: AgendaSource,
        *,
        by: str = "operator",
    ) -> None:
        self._paused_sources.discard(source)
        if self._pause_store is not None:
            self._pause_store.remove(source, by=by)

    def is_source_paused(self, source: AgendaSource) -> bool:
        """Cache-only read; the ``pause_store`` is reconciled at init and
        on every mutation, so the cache is authoritative between ticks."""
        return source in self._paused_sources

    def quiesce(self) -> None:
        """Stop accepting new selections; in-flight workers (S2) finish
        under the pre-quiesce config. Idempotent."""
        self._quiesced = True

    def resume(self) -> None:
        self._quiesced = False
        self._poke.set()

    def poke(self) -> None:
        """A publisher (sync) signals the kernel to run a tick *now*
        instead of waiting for the interval. Cheap to call repeatedly."""
        self._poke.set()

    # -- Lifecycle ---------------------------------------------------

    async def start(self) -> None:
        if self.is_running:
            return
        # Codex audit-2 2026-05-19 P2 — repair stale RUNNING / SELECTED
        # agenda items whose linked workers are already terminal on disk.
        # Without this, items left behind by pre-fix builds (where the
        # reconciler didn't exist) stay misleading forever. One-shot
        # operation at boot, then the in-process reconciler covers
        # everything from here on.
        try:
            await asyncio.to_thread(self.repair_stale_agenda_items)
        except Exception:
            log.exception("autonomy: stale-agenda repair raised — continuing boot")
        self._stopping.clear()
        self._loop_task = asyncio.create_task(self._run_loop(), name="autonomy-kernel")
        log.info(
            "autonomy: kernel started (tick=%.1fs top_k=%d)",
            self._config.tick_interval_seconds,
            self._config.top_k,
        )

    def _promote_stale_unvetted_items(self) -> dict[str, int]:
        """**UNVETTED staleness escape valve.** ``agenda.yaml::vetter.enabled``
        and ``schedule.yaml::jobs.autonomy_vetter.enabled`` are separate
        toggles. If the scheduled vetter job is off (or wedged) while
        ``vetter.enabled`` stays true, UNVETTED items would otherwise be
        invisible to selection forever — nothing else promotes them.
        This sweep runs here, in the kernel, independent of the vetter
        job's own schedule: any UNVETTED item older than
        ``config.max_unvetted_hours`` is promoted to ``PROPOSED`` so it
        re-enters the selection loop regardless of the job's state.

        Called both from :meth:`repair_stale_agenda_items` (one-shot at
        boot) AND every :meth:`tick` — the vetter job can be disabled at
        any point during a long-running kernel process, not just before
        boot, so a boot-only check would miss staleness introduced
        later. Cheap: no worker-record I/O, just an ``iter_active`` pass
        plus a config comparison.
        """
        now = self._clock()
        max_unvetted_hours = self._config.max_unvetted_hours
        checked = promoted = 0
        for item in list(self._agenda.iter_active()):
            if item.status is not AgendaStatus.UNVETTED:
                continue
            checked += 1
            if max_unvetted_hours <= 0:
                continue
            age_hours = (now - item.created_at).total_seconds() / 3600.0
            if age_hours >= max_unvetted_hours:
                self._agenda.transition(
                    item, AgendaStatus.PROPOSED,
                    reason="vet_timeout", by="kernel",
                )
                promoted += 1
        return {"unvetted_checked": checked, "unvetted_promoted": promoted}

    def _resolve_resume_queued_items(self) -> dict[str, int]:
        """**RESUME_QUEUED terminus.** Recovery parks interrupted items in
        ``RESUME_QUEUED`` on the documented promise that "the kernel
        re-derives the outcome from worker state" — but selection only
        ever walked ``PROPOSED`` items, so nothing ever picked them up.
        They accumulated silently (15 items between 2026-07-08 and
        2026-07-30 on the live install). This is that missing terminus.

        Per item: all linked workers DONE → ``DONE``. Otherwise the work
        was lost, so re-propose it — bounded by
        ``config.max_resume_attempts`` (counted from the item's own
        transition history, so the budget survives restarts). Past the
        budget the item goes ``BLOCKED`` with a reason the operator can
        see, never back to an invisible queue.

        Unloadable worker records count as lost work, not as a reason to
        leave the item parked: an item whose evidence is gone is exactly
        the case that stuck forever before. A worker still in a live
        status means this boot is running it — leave it alone.

        Called every :meth:`tick` and once from
        :meth:`repair_stale_agenda_items` at boot, same as the UNVETTED
        escape valve above.
        """
        from tesseract.orchestrator.workers.record import load_record

        resolved = {"resume_checked": 0, "resume_done": 0,
                    "resume_requeued": 0, "resume_exhausted": 0}
        for item in list(self._agenda.iter_active()):
            if item.status is not AgendaStatus.RESUME_QUEUED:
                continue
            resolved["resume_checked"] += 1

            records = []
            lost = not item.linked_workers
            for worker_id in item.linked_workers:
                try:
                    record = load_record(worker_id)
                except Exception:  # noqa: BLE001 — one bad record must not stall the sweep
                    record = None
                if record is None:
                    lost = True
                    continue
                records.append(record)
            if any(r.status not in WORKER_TERMINAL_STATUSES for r in records):
                continue  # a live worker owns this item right now

            if not lost and records and all(
                r.status is WorkerStatus.DONE for r in records
            ):
                self._agenda.transition(
                    item, AgendaStatus.DONE, reason=REASON_RESUME_ALL_DONE,
                )
                resolved["resume_done"] += 1
                continue

            attempts = sum(
                1
                for t in item.status_history
                if t.to_status is AgendaStatus.PROPOSED
                and t.reason == REASON_RESUME_RETRY
            )
            if attempts >= max(0, self._config.max_resume_attempts):
                item.blocked_reason = REASON_RESUME_EXHAUSTED
                self._agenda.transition(
                    item, AgendaStatus.BLOCKED, reason=REASON_RESUME_EXHAUSTED,
                )
                resolved["resume_exhausted"] += 1
                continue
            self._agenda.transition(
                item, AgendaStatus.PROPOSED, reason=REASON_RESUME_RETRY,
            )
            resolved["resume_requeued"] += 1
        if any(v for k, v in resolved.items() if k != "resume_checked"):
            log.info(
                "autonomy: resume-queue sweep — %s",
                ", ".join(f"{k}={v}" for k, v in resolved.items() if v),
            )
        return resolved

    def repair_stale_agenda_items(self) -> dict[str, int]:
        """Reconcile any active agenda items whose linked workers are
        already terminal. One-shot. Idempotent. Synchronous file I/O —
        run via ``asyncio.to_thread`` from async callers.

        **Missing-worker policy (conservative):** if ANY linked worker
        cannot be loaded (record file absent, unreadable, or
        ValidationError), the item is left alone. A missing record can
        signal data corruption rather than completion — synthesizing a
        terminal from the loadable subset could mask real integrity
        issues. Such items surface in the summary's
        ``items_with_missing_worker`` counter and a per-item WARNING
        log so the operator can triage manually.

        Also runs :meth:`_promote_stale_unvetted_items` and
        :meth:`_resolve_resume_queued_items` (see their docstrings) so
        both escape valves fire once at boot — right after recovery has
        parked interrupted items — in addition to every tick.

        Returns a small summary dict for logging.
        """
        from tesseract.orchestrator.workers.record import load_record

        summary = {
            "checked": 0,
            "reconciled_done": 0,
            "reconciled_blocked": 0,
            "no_workers": 0,
            "items_with_missing_worker": 0,
            "unvetted_checked": 0,
            "unvetted_promoted": 0,
        }
        summary.update(self._promote_stale_unvetted_items())
        summary.update(self._resolve_resume_queued_items())
        for item in list(self._agenda.iter_active()):
            if item.status not in (AgendaStatus.RUNNING, AgendaStatus.SELECTED):
                continue
            summary["checked"] += 1
            worker_ids = list(item.linked_workers or [])
            if not worker_ids:
                summary["no_workers"] += 1
                continue
            # Strict load: every worker ID must resolve to a record. If
            # one is missing, we don't synthesize completion — leave the
            # item alone for operator triage.
            records: list = []
            had_missing = False
            for wid in worker_ids:
                try:
                    rec = load_record(wid)
                except Exception:
                    rec = None
                if rec is None:
                    had_missing = True
                    break
                records.append(rec)
            if had_missing:
                summary["items_with_missing_worker"] += 1
                log.warning(
                    "autonomy: stale-repair leaving item %s in %s — "
                    "linked worker record(s) not loadable",
                    item.id, item.status.value,
                )
                continue
            if not all(r.status in WORKER_TERMINAL_STATUSES for r in records):
                continue
            # Pick the "worst" worker for the blocked_reason — DONE wins
            # only if every worker is DONE; otherwise the first non-DONE
            # terminal drives the BLOCKED transition.
            all_done = all(r.status is WorkerStatus.DONE for r in records)
            if all_done:
                self._agenda.transition(
                    item, AgendaStatus.DONE,
                    reason=f"stale_repair_all_done:{records[0].id}",
                )
                summary["reconciled_done"] += 1
            else:
                offender = next(r for r in records if r.status is not WorkerStatus.DONE)
                item.blocked_reason = f"stale_repair:worker_{offender.status.value}:{offender.id}"
                self._agenda.transition(
                    item, AgendaStatus.BLOCKED,
                    reason=f"stale_repair:worker_{offender.status.value}:{offender.id}",
                )
                summary["reconciled_blocked"] += 1
        if any(summary[k] for k in ("reconciled_done", "reconciled_blocked")):
            log.info(
                "autonomy: stale-agenda repair — %s",
                ", ".join(f"{k}={v}" for k, v in summary.items() if v),
            )
        return summary

    # Protocol §Tests #10 — kernel stop drains within 30s. Budget is
    # SHARED between cancelling the loop task and draining in-flight
    # dispatches so total shutdown stays bounded even when both phases
    # are slow.
    STOP_DRAIN_BUDGET_SECONDS = 30.0

    async def stop(self) -> None:
        if not self.is_running:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.STOP_DRAIN_BUDGET_SECONDS
        self._stopping.set()
        self._poke.set()
        task = self._loop_task
        self._loop_task = None
        task.cancel()
        try:
            await asyncio.wait_for(
                task, timeout=max(0.0, deadline - loop.time())
            )
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        # Drain in-flight dispatch tasks against whatever budget remains.
        # Dispatches are not cancelled (workers finish on their own); a
        # stuck worker logs and we move on rather than hang shutdown.
        if self._dispatch_tasks:
            remaining = max(0.0, deadline - loop.time())
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *self._dispatch_tasks, return_exceptions=True
                    ),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "autonomy: %d in-flight dispatch task(s) did not drain "
                    "within %.0fs shutdown budget",
                    len(self._dispatch_tasks),
                    self.STOP_DRAIN_BUDGET_SECONDS,
                )
        log.info("autonomy: kernel stopped")

    async def _run_loop(self) -> None:
        try:
            while not self._stopping.is_set():
                try:
                    await self.tick()
                except Exception:
                    log.exception("autonomy: tick raised — continuing")
                # Sleep until interval elapses OR a publisher pokes us.
                try:
                    await asyncio.wait_for(
                        self._poke.wait(),
                        timeout=self._config.tick_interval_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
                self._poke.clear()
        except asyncio.CancelledError:
            return

    # -- Tick --------------------------------------------------------

    async def tick(self) -> KernelTickResult:
        """One cycle of the per-tick flow. Idempotent — calling tick
        twice with no new events MUST yield zero new items the second
        time (dedupe holds)."""
        result = KernelTickResult()
        events = self._bus.drain()
        result.events_drained = len(events)

        # Steps 2 + 3: events → drafts → items.
        for event in events:
            drafts = self._run_mapper(event)
            result.drafts_emitted += len(drafts)
            for draft in drafts:
                created, deduped = self._persist_draft(draft)
                if created:
                    result.items_created += 1
                elif deduped:
                    result.items_deduped += 1

        # Fix 1 escape valve — run every tick, not just at boot, so an
        # UNVETTED item does not depend on the vetter job's schedule
        # staying enabled for the lifetime of a long-running kernel.
        self._promote_stale_unvetted_items()
        # Same reasoning for RESUME_QUEUED: an item parked mid-tick (worker
        # crash, kernel restart under a live process) must not wait for the
        # next boot to be re-derived.
        self._resolve_resume_queued_items()

        # Step 5: daily caps + global concurrency. If we're paused for
        # the day we still drained + persisted (recovery resumes the
        # work later) but we do not select anything new.
        cap_block = self._check_daily_caps()
        if cap_block:
            result.paused = True
            result.pause_reason = cap_block
            self._dispatch_paused = True
            self._dispatch_pause_reason = cap_block
            return result
        if self._quiesced:
            result.paused = True
            result.pause_reason = "quiesced"
            return result
        self._dispatch_paused = False
        self._dispatch_pause_reason = None

        # Steps 6 + 7: selection + dispatch. Pulls ranked items, walks
        # top-down, filters governor-paused sources, admits via the lane,
        # generates rationale, writes WorkerRecord, hands record to the
        # injected runner as a background task.
        result.selected, result.rejections = await self._select_and_dispatch()
        return result

    # -- Mapper dispatch ---------------------------------------------

    def _is_enabled(self, source: AgendaSource) -> bool:
        cfg = self._mapper_configs.get(source)
        if cfg is None:
            # Operator-source events fire even without an explicit
            # config entry — they're operator-typed, never noise.
            return source == AgendaSource.OPERATOR
        return cfg.enabled

    def _run_mapper(self, event: AutonomyEvent) -> list[AgendaItemDraft]:
        if not self._is_enabled(event.source):
            return []
        # AU-6 — Governor hook. Per ``phase-AU-6 §7 step 4``, mappers
        # short-circuit when their source is paused so events do not even
        # become agenda items. Items minted before the pause still get
        # filtered at selection (``_select_and_dispatch`` below), so the
        # protection is defence-in-depth.
        if event.source in self._paused_sources:
            return []
        mapper = self._mappers.get(event.source)
        if mapper is None:
            log.debug("autonomy: no mapper for source %s", event.source.value)
            return []
        try:
            drafts = list(mapper(event))
        except Exception:
            log.exception("autonomy: mapper for %s raised", event.source.value)
            return []
        return drafts

    def _persist_draft(self, draft: AgendaItemDraft) -> tuple[bool, bool]:
        """Returns ``(created, deduped)``. ``absolute_deny`` drafts are
        rejected at the store layer; we treat that as ``(False, False)``
        and log."""
        # Dedupe by (source, source_event_id) first so the same event
        # replayed doesn't create new items. Then fall back to the
        # (source, normalised goal) dedupe the store ships with.
        if draft.source_event_id:
            for existing in self._agenda.iter_active():
                if existing.source_event_id == draft.source_event_id:
                    return False, True
        existing = self._agenda.find_dedupe(draft.goal, draft.source)
        if existing is not None:
            return False, True

        if draft.source not in (AgendaSource.OPERATOR, AgendaSource.OPERATOR_VIEW):
            now = self._clock()

            def _prune(stage: PruneStage, reason: str) -> None:
                record_prune(
                    PruneRecord(
                        item_id=None,
                        source=draft.source,
                        goal=draft.goal[:500],
                        stage=stage,
                        reason=reason,
                        ts=now,
                    )
                )

            # 1. structurally-degenerate goal (e.g. a lone "}")
            if is_degenerate_goal(draft.goal):
                _prune(PruneStage.MALFORMED, "degenerate goal")
                return False, True
            # 2. fuzzy near-duplicate. Window: the mapper's own
            # dedupe_window_hours when configured (was parsed-but-inert
            # since inception — Deferred 2026-07-12), else the kernel-wide
            # fuzzy_window_hours.
            mapper_cfg = self._mapper_configs.get(draft.source)
            window_hours = (
                mapper_cfg.dedupe_window_hours
                if mapper_cfg is not None
                else self._config.fuzzy_window_hours
            )
            dup = self._agenda.find_fuzzy_dedupe(
                draft.goal,
                draft.source,
                threshold=self._config.fuzzy_threshold,
                window_hours=window_hours,
                now=now,
            )
            if dup is not None:
                _prune(PruneStage.DUPLICATE, f"fuzzy dup of {dup.id}")
                return False, True
            # 3. caps
            if self._agenda.count_open_total() >= self._config.max_open_total:
                _prune(PruneStage.CAPPED, "max_open_total reached")
                return False, True
            if (
                self._agenda.count_open_by_source(draft.source)
                >= self._config.max_open_per_source
            ):
                _prune(
                    PruneStage.CAPPED,
                    f"max_open_per_source reached for {draft.source.value}",
                )
                return False, True

        status = AgendaStatus.PROPOSED
        if self._config.vet_enabled and draft.source in self._config.vet_required:
            status = AgendaStatus.UNVETTED
        item = draft.to_item(now=self._clock(), status=status)
        try:
            self._agenda.add(item)
        except ValueError as exc:
            log.warning("autonomy: agenda.add refused draft: %s", exc)
            return False, False
        return True, False

    # -- Selection + dispatch ----------------------------------------

    async def _select_and_dispatch(
        self,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Walk the ranked queue, admit up to ``top_k`` items, dispatch
        each to a background worker task. Returns the per-item
        structured results for the dashboard. Per GOVERNANCE §6 the
        durable :class:`WorkerRecord` is written *before* the runner
        starts so recovery sees a complete picture even if the runner
        crashes mid-spawn."""
        ranked = self._agenda.ranked()
        selected: list[str] = []
        rejections: list[dict[str, Any]] = []
        running_total = self._running_worker_total()
        for item in ranked:
            if len(selected) >= self._config.top_k:
                break
            if item.status != AgendaStatus.PROPOSED:
                continue
            if item.source in self._paused_sources:
                self._agenda.transition(
                    item,
                    AgendaStatus.BLOCKED,
                    reason=REASON_GOVERNOR_PAUSED,
                    by="governor",
                )
                rejections.append(
                    {"id": item.id, "reason": REASON_GOVERNOR_PAUSED}
                )
                continue

            if not _approvals_satisfied(item):
                self._agenda.transition(
                    item,
                    AgendaStatus.AWAITING_OPERATOR,
                    reason=REASON_AWAITING_OPERATOR,
                )
                rejections.append(
                    {"id": item.id, "reason": REASON_AWAITING_OPERATOR}
                )
                continue

            if running_total >= self._config.max_concurrent_workers_total:
                rejections.append(
                    {
                        "id": item.id,
                        "reason": REASON_TOTAL_CONCURRENCY_BLOCK,
                        "running": running_total,
                        "cap": self._config.max_concurrent_workers_total,
                    }
                )
                break

            kind = _kind_for_item(item)
            if kind is None:
                rejections.append({"id": item.id, "reason": REASON_RISK_MISMATCH})
                continue
            decision = self._lane.admit(kind=kind, risk_class=item.risk_class)
            if not decision.admitted:
                rejections.append(self._reject_row(item, decision))
                continue

            await self._dispatch_item(item, kind=kind)
            # Audit-1 follow-up (2026-05-24): ``_dispatch_item`` may halt
            # the item in BLOCKED without spawning a worker (worktree
            # fail-closed). Account for that here so the tick result
            # reports a rejection instead of falsely claiming success,
            # and so the within-tick concurrency cap isn't burned by a
            # dispatch that never started.
            if item.status is AgendaStatus.BLOCKED:
                rejections.append(
                    {"id": item.id, "reason": item.blocked_reason or "blocked"}
                )
                continue
            selected.append(item.id)
            running_total += 1
        return selected, rejections

    async def _dispatch_item(self, item: AgendaItem, *, kind: WorkerKind) -> None:
        """Transition the item to ``RUNNING`` via the ``SELECTED``
        intermediate, mint + persist the WorkerRecord, link it on the
        item, then spawn the runner as a tracked background task.
        Rationale generation is best-effort — failure falls through to
        :data:`UNAVAILABLE_MARKER` so dispatch never hinges on the
        model layer."""
        peers = self._agenda.ranked()
        rationale = await generate_rationale(
            item, peers, adapter=self._rationale_adapter
        )
        # ``generate_rationale`` always returns a non-empty string —
        # either the model's text or ``UNAVAILABLE_MARKER``. Both go on
        # ``last_decision`` so the operator sees *why* even when the
        # model layer was offline. Only the real text is mirrored to
        # ``item.rationale`` (which is operator-visible).
        item.last_decision = rationale[:2000]
        if rationale != UNAVAILABLE_MARKER:
            item.rationale = rationale[:2000]
        self._agenda.transition(
            item, AgendaStatus.SELECTED, reason=REASON_SELECTED
        )

        # ``record.role`` is the *agent slug pin* — only set when the
        # agenda item explicitly requests a specific agent (today: never
        # from the kernel itself). Codex audit 2026-05-19 P0 #1 caught
        # the prior behaviour where we passed ``self._rationale_role``
        # (a *model role* name like "agents_default") into the agent-slug
        # field, and the runner then called ``invoke_agent(name="agents_default")``
        # which failed every dispatch with ``Unknown agent: 'agents_default'``.
        # The runner falls back to ``DEFAULT_TARS_SELF_AGENT`` / kind-specific
        # defaults when this field is empty.
        record = build_worker_record(
            item, kind=kind, role="", now=self._clock()
        )
        # Audit-2 M-3 / audit-1 follow-up (2026-05-24): allocate an
        # isolated git worktree for code-editing risk classes + kinds
        # (PROPOSE / OPERATOR_GATE × CLAUDE_CLI / CODEX_CLI). The path
        # lands on the record BEFORE ``write_record`` so the SPAWNING
        # row already names the worktree the runner will execute inside.
        # Read-only kinds (markdown_agent / tars_self) and AUTONOMOUS
        # items return ``None`` and dispatch unchanged.
        #
        # Fail-closed policy: if allocation raises (git missing, path
        # collision, permission, disk full), the item halts in BLOCKED
        # with ``blocked_reason=worktree_alloc_failed:<class>:<exc>``.
        # The runner is NOT spawned — isolation is the whole point of
        # the worktree, so a code-editing worker must never touch the
        # live tree just because allocation tripped. The operator
        # resolves the underlying issue (install git, free disk, clear
        # permissions) and unblocks; the agenda item resumes from
        # BLOCKED on the next tick.
        try:
            await asyncio.to_thread(_allocate_worktree_for_record, record)
        except (WorktreeError, OSError) as exc:
            reason = f"worktree_alloc_failed:{type(exc).__name__}:{exc}"
            log.warning(
                "autonomy: worktree allocation failed for item %s — "
                "halting in BLOCKED (%s)",
                item.id, reason,
            )
            item.blocked_reason = reason[:2000]
            self._agenda.transition(
                item, AgendaStatus.BLOCKED, reason=reason,
            )
            return
        record.transition_to(WorkerStatus.SPAWNING, reason="kernel_dispatch")
        write_record(record)
        item.linked_workers.append(record.id)
        self._agenda.transition(
            item, AgendaStatus.RUNNING, reason=f"worker_{record.id}"
        )
        register_autonomy(item.id, label=item.goal)

        operator_journal.append(
            "dispatch",
            {
                "agenda_item_id": item.id,
                "worker_id": record.id,
                "summary": item.goal,
                "worker_kind": kind.value,
                "risk_class": item.risk_class.value,
            },
        )

        task = asyncio.create_task(
            self._run_worker(record), name=f"autonomy-worker:{record.id}"
        )
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._dispatch_tasks.discard)

    async def _run_worker(self, record) -> None:
        """Drive the injected runner. Records the terminal state if the
        runner raises so the kernel's invariant — every dispatch leaves
        a durable record — holds even when the runner is broken.

        Codex audit 2026-05-19 P0 #2: also reconciles the linked agenda
        item once the runner terminates. Without this the dashboard
        showed RUNNING agenda items linked to FAILED workers — TARS
        looked busy while doing nothing. DONE workers transition the
        item to DONE; non-DONE terminals transition to BLOCKED with
        ``blocked_reason=worker_<status>:<id>`` so the operator can
        decide whether to retry or cancel.
        """
        try:
            try:
                await self._runner.run(record)
            except Exception:
                log.exception("autonomy: runner raised for %s", record.id)
                record.transition_to(WorkerStatus.FAILED, reason="runner_raised")
                record.error_class = "RunnerException"
                try:
                    write_record(record)
                except Exception:
                    log.exception("autonomy: failed to persist FAILED record %s", record.id)

            # Audit-2 M-3: finalize the worktree on terminal status so the
            # diff is captured and the live worktree is archived. Idempotent;
            # ``None`` when no worktree was allocated for this record.
            if record.status in WORKER_TERMINAL_STATUSES:
                try:
                    await asyncio.to_thread(_finalize_worktree_for_record, record)
                except (WorktreeError, OSError):
                    log.exception(
                        "autonomy: worktree finalize raised for %s", record.id,
                    )

            try:
                self._reconcile_agenda_for_worker(record)
            except Exception:
                log.exception(
                    "autonomy: agenda reconcile raised for worker %s (item %s)",
                    record.id, record.agenda_item_id,
                )
        finally:
            # HUD runs-surface fix (2026-07-02): must run on every exit path,
            # including asyncio.CancelledError (task cancellation / shutdown),
            # or a stuck worker leaves the activity record ``running`` forever
            # — sweep_terminal_ephemeral only evicts terminal states.
            #
            # Operator must not lose a FAILED worker to a silent chip
            # disappearance (2026-07-05): a FAILED record transitions the
            # chip to ``failed`` (stays until dismissed) instead of removing
            # it. Every other terminal/non-terminal exit (DONE, BLOCKED,
            # INTERRUPTED, CANCELLED, or a bare task cancellation that never
            # reached a terminal status) keeps the prior remove behavior.
            if record.status == WorkerStatus.FAILED:
                detail = record.error_message or record.error_class or "worker failed"
                fail_autonomy(record.agenda_item_id, detail=detail)
            else:
                remove_autonomy(record.agenda_item_id)

    def _reconcile_agenda_for_worker(self, record) -> None:
        """Close the linked agenda item when its worker reaches terminal.

        Only acts when the worker is in a terminal state AND the agenda
        item is still in a pre-terminal, kernel-owned status
        (``RUNNING`` / ``SELECTED``). Operator-driven states
        (``AWAITING_OPERATOR``, ``CANCELLED``, ``DONE``, ...) are left
        untouched — the operator surface owns those.

        Non-terminal worker states (``QUEUED``, ``SPAWNING``, ``RUNNING``)
        are no-ops — a runner returning without driving the record to a
        terminal status means it left the work in-flight (or the runner
        is the default ``_NoopRunner`` used in tests / before AU-12 CLI
        runners land). The reconciler must not synthesize a transition
        in that case.
        """
        if record.status not in WORKER_TERMINAL_STATUSES:
            return

        # Record outcome before agenda reconciliation; advice_only also fires
        # when the worker produced a summary but no artifacts (silent-advisor).
        artifacts_count = len(record.artifacts or [])
        summary = (record.summary or "").strip()
        operator_journal.append(
            "outcome",
            {
                "agenda_item_id": record.agenda_item_id or None,
                "worker_id": record.id,
                "status": record.status.value,
                "summary": summary or None,
                "artifacts": artifacts_count,
            },
        )
        if (
            record.status is WorkerStatus.DONE
            and summary
            and artifacts_count == 0
        ):
            operator_journal.append(
                "advice_only",
                {
                    "agenda_item_id": record.agenda_item_id or None,
                    "worker_id": record.id,
                    "summary": summary,
                    "artifacts": 0,
                },
            )
            # TC-7 — try to draft a follow-up agenda item from this
            # advisor's summary. The mapper never raises; a None return
            # means non-actionable / disabled / persistence failed.
            try:
                self._follow_up_mapper.create_draft_if_actionable(record)
            except Exception:  # noqa: BLE001 — must not poison reconcile
                log.exception(
                    "autonomy: follow_up_mapper raised for worker %s",
                    record.id,
                )

        item_id = (record.agenda_item_id or "").strip()
        if not item_id:
            return
        item = self._agenda.get(item_id)
        if item is None:
            log.warning(
                "autonomy: worker %s references unknown agenda item %s — skipping reconcile",
                record.id, item_id,
            )
            return
        if item.status not in (AgendaStatus.RUNNING, AgendaStatus.SELECTED):
            return

        if record.status is WorkerStatus.DONE:
            self._agenda.transition(
                item, AgendaStatus.DONE,
                reason=f"worker_done:{record.id}",
            )
            return

        # Non-DONE terminals (FAILED / BLOCKED / INTERRUPTED / CANCELLED)
        # land the item in BLOCKED with a structured reason. Persisted on
        # the item's blocked_reason field so the dashboard can surface it.
        item.blocked_reason = f"worker_{record.status.value}:{record.id}"
        self._agenda.transition(
            item, AgendaStatus.BLOCKED,
            reason=f"worker_{record.status.value}:{record.id}",
        )

    @staticmethod
    def _reject_row(item: AgendaItem, decision: AdmissionResult) -> dict[str, Any]:
        reason_map = {
            AdmissionDecision.REJECT_LANE_FULL: REASON_LANE_FULL,
            AdmissionDecision.REJECT_RISK_MISMATCH: REASON_RISK_MISMATCH,
            AdmissionDecision.REJECT_UNCONFIGURED: REASON_LANE_UNCONFIGURED,
        }
        return {
            "id": item.id,
            "reason": reason_map.get(decision.decision, decision.decision.value),
            "running": decision.running,
            "cap": decision.cap,
        }

    @staticmethod
    def _running_worker_total() -> int:
        """Count records in non-terminal workers/active/ via the same
        cheap status summary the lane uses. Decoupled from any specific
        kind so the kernel can enforce the global ceiling."""
        terminal = {s.value for s in WORKER_TERMINAL_STATUSES}
        return sum(
            1
            for _, _, status in iter_active_status_summary()
            if status not in terminal
        )

    # -- Daily caps --------------------------------------------------

    def _check_daily_caps(self) -> str | None:
        """Return a pause reason string when a cap has been hit, else
        ``None``. The kernel does NOT halt event ingestion — it stops
        emitting selections so in-flight workers complete cleanly.
        """
        tokens_cap = self._config.daily_tokens_cap
        seconds_cap = self._config.daily_seconds_cap
        usd_cap = self._config.daily_usd_cap
        if tokens_cap <= 0 and seconds_cap <= 0 and usd_cap <= 0:
            return None
        spend = self._agenda.today_spend()
        if tokens_cap > 0 and spend.get("tokens", 0) >= tokens_cap:
            return REASON_DAILY_CAP_PAUSE
        if seconds_cap > 0 and spend.get("seconds", 0) >= seconds_cap:
            return REASON_DAILY_CAP_PAUSE
        # F6 — global daily USD ceiling. Only enforced when both a cap and a
        # ledger accessor are present; a broken accessor must not wedge the
        # kernel, so a failed read is logged and treated as "under cap".
        if usd_cap > 0 and self._daily_usd_spent is not None:
            try:
                spent_usd = self._daily_usd_spent()
            except Exception:
                log.exception("autonomy: daily USD accessor failed — skipping USD cap this tick")
            else:
                if spent_usd >= usd_cap:
                    return REASON_DAILY_CAP_PAUSE
        return None


def _approvals_satisfied(item: AgendaItem) -> bool:
    if not item.approvals_required:
        return True
    return all(gate.fulfilled for gate in item.approvals_required)


def build_kernel_from_configs(
    *,
    agenda_yaml: Path,
    mappers_yaml: Path,
    worker_lane: WorkerLane,
    pause_store: PauseStore | None = None,
    worker_runner: WorkerRunner | None = None,
    daily_usd_spent: Callable[[], float] | None = None,
) -> AutonomyKernel:
    """Convenience used by the Mirror lifecycle. Reads both YAML files,
    builds the store + kernel. Caller is responsible for ``start``.

    A ``pause_store`` may be injected so AU-6 governor pauses survive
    backend restart; when omitted the kernel falls back to its in-memory
    set (test fixtures stay light).

    ``worker_runner`` defaults to the ``_NoopRunner`` (mark DONE
    immediately, leave a durable record). Production should inject
    :class:`KernelWorkerRunner` (or any other concrete runner)."""
    raw = yaml.safe_load(agenda_yaml.read_text(encoding="utf-8")) or {}
    weights = load_weights_from_yaml(agenda_yaml)
    config = KernelConfig.from_yaml_dict(raw)
    mapper_configs = load_mapper_configs(mappers_yaml)
    follow_up_config = FollowUpConfig.from_yaml_block(
        raw.get("follow_up_mapper")
    )
    store = AgendaStore(weights=weights)
    follow_up_mapper = FollowUpMapper(store, follow_up_config)
    return AutonomyKernel(
        agenda_store=store,
        worker_lane=worker_lane,
        config=config,
        mapper_configs=mapper_configs,
        pause_store=pause_store,
        worker_runner=worker_runner,
        follow_up_mapper=follow_up_mapper,
        daily_usd_spent=daily_usd_spent,
    )


__all__ = [
    "AutonomyKernel",
    "DEFAULT_MAX_CONCURRENT_TOTAL",
    "DEFAULT_TICK_SECONDS",
    "DEFAULT_TOP_K",
    "KernelConfig",
    "KernelTickResult",
    "MapperConfig",
    "REASON_AWAITING_OPERATOR",
    "REASON_DAILY_CAP_PAUSE",
    "REASON_DEDUPE_HIT",
    "REASON_GOVERNOR_PAUSED",
    "REASON_LANE_FULL",
    "REASON_LANE_UNCONFIGURED",
    "REASON_RISK_MISMATCH",
    "REASON_SELECTED",
    "REASON_TOTAL_CONCURRENCY_BLOCK",
    "build_kernel_from_configs",
    "load_mapper_configs",
]
