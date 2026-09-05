from __future__ import annotations

import asyncio
import importlib
import logging
import re
import time
import uuid
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from croniter import croniter

from tesseract.context.circuit_breaker import _default_max_failures
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.cadence import INTERVAL_RE as _INTERVAL_RE, parse_interval as _parse_interval
from tesseract.scheduler.config_loader import (
    JobConfig,
    RetryPolicy,
    load_schedule_config,
    persist_job_add,
    persist_job_remove,
    persist_job_update,
    refuse_cadence,
    system_rows,
)
from tesseract.scheduler.log import append_run_log, load_last_runs
from tesseract.scheduler.manifest.entry import MIN_SUMMARY_CHARS
from tesseract.scheduler.triggers import (
    check_row as check_trigger_row,
    evaluate as evaluate_trigger,
    record_fired as record_trigger_fired,
)
from tesseract.scheduler.types import TRIGGER_SOURCES, JobContext, JobResult
from tesseract.orchestrator.activity.hooks import fail_routine, register_routine, remove_routine

log = logging.getLogger(__name__)

MAX_CONSECUTIVE_FAILURES = _default_max_failures()


class AlreadyRunning(RuntimeError):
    """A hand-fired run was asked for while one is already in flight.

    Its own type because the caller's answer differs from every other refusal
    here: nothing is wrong, the job is simply already doing the thing, and a
    surface should say that rather than report a failure.
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"{name} is already running")
        self.name = name
TICK_SECONDS = 60
ALARM_TICK_SECONDS = 10

# Handler whitelist. Agent-authored or REST-authored
# `schedule_create` calls must reference a class under one of these
# module prefixes; arbitrary import paths are refused. Operators can
# extend the whitelist by editing this constant or by adding a new
# module under `tesseract.scheduler.tasks`.
ALLOWED_HANDLER_PREFIXES: tuple[str, ...] = (
    "tesseract.scheduler.tasks.",
)

def _validate_model_role(
    job_name: str, model_role: str | None, handler_cls: type[BaseJob]
) -> None:
    """Loud-fail when a job's model_role can't be honored.

    schedule.yaml is the operator's single source of truth for which
    role each LLM job uses; roles.yaml is the canonical role catalog.
    Both must be in sync — if they're not, boot stops. There is no
    silent fallback to the handler's default when an explicit override
    is set but unresolvable: that would let a typo run for weeks before
    anyone noticed the wrong model was being charged.

    Skipped only when `model_role` is None (handler default is the
    intentional config). A missing roles.yaml or a missing role name
    raises — the operator must reconcile before the engine arms.
    """
    if model_role is None:
        return
    if not getattr(handler_cls, "uses_llm", False):
        raise RuntimeError(
            f"schedule.yaml: job {job_name!r} sets model_role={model_role!r} "
            f"but its handler does not use an LLM"
        )
    from tesseract.brain.boot import load_bundle
    bundle = load_bundle()
    if model_role not in bundle.roles:
        raise RuntimeError(
            f"schedule.yaml: job {job_name!r} references role {model_role!r} "
            f"which is not defined in roles.yaml — reconcile both files before booting"
        )


@dataclass
class _JobRuntime:
    cfg: JobConfig
    handler_cls: type[BaseJob]
    interval_seconds: int | None
    enabled: bool
    consecutive_failures: int = 0
    last_result: JobResult | None = None
    last_fired_at: datetime | None = None
    # What caused the last run. `None` means this process has not fired the
    # job yet — `last_fired_at` may still be set, seeded from `runs.jsonl` at
    # boot, and that record's trigger is not carried into memory. The Schedule
    # view renders the difference rather than guessing.
    last_trigger: str | None = None
    # Trigger rows only. `fired_this_process` is the whole position of the
    # `boot` condition; `when_reason` is the condition's own sentence about
    # the last time it was asked, kept so a row that is waiting can say what
    # it is waiting for instead of looking identical to one that is stuck.
    fired_this_process: bool = False
    when_reason: str = ""


@dataclass
class SchedulerEngine:
    config_dir: Path
    log_dir: Path | None = None
    registry: dict[str, _JobRuntime] = field(default_factory=dict)
    pending_alerts: list[JobResult] = field(default_factory=list)
    # What the pipeline's config boot checks found when this engine armed.
    # Empty until `start`; a non-empty list is a declared dependency that has
    # no producer, and it is left readable rather than only logged.
    boot_findings: list[str] = field(default_factory=list)
    tick_seconds: int = TICK_SECONDS
    alarm_tick_seconds: int = ALARM_TICK_SECONDS
    stop_join_timeout: float = 5.0  # seconds: how long `stop()` waits for in-flight jobs
    _task: asyncio.Task | None = None
    _alarm_task: asyncio.Task | None = None
    _app: Any = None
    _stopping: asyncio.Event = field(default_factory=asyncio.Event)
    # Every spawned `_run_job(...)` task is tracked here so `stop()` can
    # await or cancel them deterministically. Mirror-initiated `run_now`
    # tasks register via `spawn_tracked_task` instead of bare
    # `asyncio.create_task`.
    _inflight: set[asyncio.Task] = field(default_factory=set)
    # Jobs whose boot catch-up replay is still queued behind the catch-up
    # semaphore or running. `_tick` skips these — with a narrow semaphore a
    # replay can wait minutes, long past the 60s `last_fired_at` dedupe, and
    # a `*/5` cron job would otherwise double-fire ungated mid-queue.
    _catchup_pending: set[str] = field(default_factory=set)
    # Jobs a run is inside right now, by whichever door it came through.
    # `_run_job` owns this: the guard began on `run_now` alone, which covered
    # the panel's button against the `schedule_run` tool and left the commoner
    # collision open — a six minute run started at 22:59 was still going when
    # the 23:00 tick fired the same job, and both ran.
    _running: set[str] = field(default_factory=set)
    # Where trigger rows keep their position. Injectable so a test can point
    # it at `tmp_path`; built on first use rather than at construction because
    # its default path resolves `TESSERACT_HOME` and an engine is constructed
    # in places that have not set one yet.
    watermarks: Any = None

    def __post_init__(self) -> None:
        cfg = load_schedule_config(self.config_dir)
        self._catchup_concurrency = cfg.catchup.concurrency
        for job_cfg in cfg.jobs:
            handler_cls = self._resolve_handler(job_cfg.handler)
            # Force placeholder jobs disabled regardless of yaml `enabled`.
            # Without this, `_tick` fires the placeholder every minute until
            # the breaker disables it — audit-1 m1 (2026-04-24).
            enabled = job_cfg.enabled and handler_cls is not _PlaceholderJob
            _validate_model_role(job_cfg.name, job_cfg.model_role, handler_cls)
            if job_cfg.when:
                # Same class of error as an unresolvable `model_role`, and
                # raised in the same place: a row whose condition does not
                # exist, or whose threshold is missing, would sit in the
                # registry looking armed and never fire.
                check_trigger_row(job_cfg.name, job_cfg.when, job_cfg.when_config)
            self.registry[job_cfg.name] = _JobRuntime(
                cfg=job_cfg,
                handler_cls=handler_cls,
                interval_seconds=_parse_interval(job_cfg.cadence),
                enabled=enabled,
            )

    def _watermark_store(self) -> Any:
        if self.watermarks is None:
            from tesseract.scheduler.pipeline.artifacts import WatermarkStore

            self.watermarks = WatermarkStore()
        return self.watermarks

    @staticmethod
    def _resolve_handler(dotted: str) -> type[BaseJob]:
        """Import a handler class by dotted path.

        Missing modules/attrs are tolerated at init — the job stays disabled and
        logs a warning. This lets S0 ship before S1–S4 create the concrete task
        modules referenced in schedule.yaml.
        """
        module_path, _, cls_name = dotted.rpartition(".")
        if not module_path:
            raise ValueError(f"handler path must be dotted: {dotted!r}")
        try:
            module = importlib.import_module(module_path)
            cls = getattr(module, cls_name)
        except (ImportError, AttributeError) as exc:
            log.warning("scheduler: handler %s not yet available (%s); job will remain disabled", dotted, exc)
            return _PlaceholderJob
        if not isinstance(cls, type) or not issubclass(cls, BaseJob):
            raise TypeError(f"{dotted} must subclass BaseJob")
        return cls

    def _regate_rows(self) -> list[str]:
        """Ask the tool gate about every armed row, now that there is an app.

        `__post_init__` cannot: it runs before `start`, so there is no app and
        no registry to resolve a posture against, and a row read off disk comes
        up exactly as the file wrote it. That leaves a window where a row
        naming a tool the operator keeps behind a prompt reads as ON, in the
        Schedule view and in Managed system, until its first fire refuses it.
        Nothing ungated ever ran in that window, because the job checks again
        when it fires. What was wrong was the answer the panel gave.

        Safe to ask here, and `config/boot.yaml` is why: `tool_registry` is in
        the `core` layer and the scheduler is in `wiring`, layers run in
        sequence, so the registry is built and its policy attached before this
        is reached. If that ever stops being true, `_armable` says nothing
        rather than disarming, and the fire-time check still holds the line.
        """
        refused: list[str] = []
        for name, rt in self.registry.items():
            if not rt.enabled:
                continue
            if self._armable(rt.cfg, refused):
                continue
            rt.enabled = False
            rt.cfg = rt.cfg.model_copy(update={"enabled": False})
            # Written down, not just held in memory. Otherwise the file still
            # says the row is on, every boot reads it, turns it off again and
            # files another identical card: the repetition the fire-time path
            # was already fixed for. Persisting makes the second boot a no-op,
            # which is the dedupe, and makes the file agree with the panel.
            #
            # So the card names only what the write actually took. A row whose
            # write failed will be disarmed again next boot, and announcing it
            # now would announce it again then, which is the thing being
            # avoided. The failure goes to the log, where it belongs.
            try:
                self._persist_job(name, {"enabled": False})
            except Exception:  # noqa: BLE001 — the row is off either way
                log.exception("scheduler: could not write %s off", name)
                refused.remove(name)
        if refused:
            log.warning(
                "scheduler: %d row(s) came up off, the tool they run needs "
                "approval: %s",
                len(refused),
                ", ".join(refused),
            )
        return refused

    async def _say_which_rows_came_up_off(self, refused: list[str]) -> None:
        """Tell the operator about a row boot turned off.

        A log line is not telling anybody. The reload door renders its
        refusals into a toast because a person is at the screen when a reload
        happens; boot has no session to toast, and the row will not fire, so
        the fire-time card never comes either. That leaves the Workspace,
        which is where a decision waits regardless of who is connected.

        Best effort throughout. A row that could not be announced is still
        correctly off, and a boot must not fail over a card.
        """
        if not refused:
            return
        try:
            from tesseract.paths import home_logs_root
            from tesseract.workspace_events import EventStore, WorkspaceEvent

            rows = ", ".join(refused)
            event = WorkspaceEvent.new(
                kind="nudge",
                source="agent",
                title=f"{len(refused)} scheduled job(s) came up off",
                summary=(
                    f"{rows} run tools that need your approval, so they were "
                    "turned off at startup rather than failing every time they "
                    "came round. Set the tool to auto in permissions.yaml if "
                    "you want it running on its own, then switch the job back "
                    "on."
                ),
                payload={"origin": "scheduler_boot", "jobs": list(refused)},
            )
            EventStore(home_logs_root()).append_event(event)
        except Exception:  # noqa: BLE001 — the rows are off either way
            log.exception("scheduler: could not announce the rows that came up off")

    async def start(self, app: Any) -> None:
        self._app = app
        self._stopping.clear()
        # The manifest describes what the app SHIPS, so it is checked against
        # the app tree and not against this engine's `config_dir` — a harness
        # pointed at four rows in `tmp_path` is not a machine whose declaration
        # is wrong.
        #
        # It raises where `_run_boot_checks` reports. Those cover config whose
        # other half this repo does not own; this covers a declaration it owns
        # entirely, and a row nothing declares is exactly the state the manifest
        # exists to make impossible — arming anyway would run work the system
        # cannot describe. `_start_scheduler` fail-opens, so the backend still
        # boots and says why there is no scheduler.
        from tesseract.scheduler.manifest import verify_live

        verify_live()
        self.boot_findings = self._run_boot_checks()
        await self._say_which_rows_came_up_off(self._regate_rows())
        now = datetime.now(timezone.utc)
        # Owner request 2026-04-29 — Mirror schedule view should show the
        # persisted last-run timestamp on boot. `_compute_catchup` already
        # reads `runs.jsonl` for catch-up math; seed `rt.last_fired_at`
        # from the same source so `runtime_state` returns it in the
        # `/api/schedule` payload (was None for any job that hadn't fired
        # in this process).
        persisted_last_runs = load_last_runs(self.log_dir)
        for name, last in persisted_last_runs.items():
            rt = self.registry.get(name)
            if rt is not None and rt.last_fired_at is None:
                rt.last_fired_at = last
        catchup_jobs = self._compute_catchup(now)
        # Batch the replay: a restart after a missed window can queue 30+
        # jobs, and firing them all at once saturates the shared free-tier
        # providers (2026-07-13: NIM 429 cascade tripped the vault_lint
        # breaker mid-run). The semaphore admits `catchup.concurrency`
        # jobs at a time; the rest start as slots free up.
        catchup_sem = asyncio.Semaphore(self._catchup_concurrency)

        async def _gated_catchup(name: str, rt: _JobRuntime) -> None:
            try:
                async with catchup_sem:
                    # Re-stamp at actual start so the 60s tick dedupe is
                    # anchored to real execution, not queue-entry time.
                    if not self._claim(name):
                        log.info(
                            "scheduler: %s is already running, so its catchup is skipped",
                            name,
                        )
                        return
                    rt.last_fired_at = datetime.now(timezone.utc)
                    await self._run_job(name, rt, now, trigger="catchup")
            finally:
                self._catchup_pending.discard(name)

        for name in catchup_jobs:
            rt = self.registry[name]
            rt.last_fired_at = now
            self._catchup_pending.add(name)
            self.spawn_tracked_task(
                _gated_catchup(name, rt),
                name=f"scheduler-catchup-{name}",
            )
        self._task = asyncio.create_task(self._tick_loop(), name="scheduler-tick")
        self._alarm_task = asyncio.create_task(self._alarm_tick_loop(), name="scheduler-alarm-tick")
        log.info(
            "scheduler started: %d job(s) registered, %d catch-up fire(s)",
            len(self.registry),
            len(catchup_jobs),
        )

    def _run_boot_checks(self) -> list[str]:
        """The pipeline's config checks, at the moment scheduled work arms.

        Reported rather than fatal: three agenda sources have no producer on
        this tree today, and a backend that refused to boot until they were
        wired would trade a silent gap for an unusable app. Each is either
        wired or deleted; until then boot says so every time, and the list
        stays readable for the health surface.
        """
        try:
            from tesseract.scheduler.pipeline.checks import (
                PIPELINE_ROW_JOBS,
                log_config_checks,
            )

            # Includes the pipeline's own graph check. It lives here rather
            # than at import of the stages package so a bad declaration
            # disables the two pipeline rows and nothing else — raised at
            # import it escaped this constructor and left the machine with no
            # scheduler at all.
            findings = log_config_checks(self.config_dir)
            if any(f.check == "row_declaration" for f in findings):
                # Reporting it is not enough: a row whose graph cannot be
                # ordered raises inside PipelineRunner on every tick, which is
                # a failing job every five minutes and a tripped breaker, not
                # a disabled row. Disable them here and say so once.
                for name in PIPELINE_ROW_JOBS:
                    rt = self.registry.get(name)
                    if rt is not None and rt.enabled:
                        rt.enabled = False
                        log.error(
                            "scheduler: %s disabled — the pipeline declaration "
                            "does not hold; every other job still runs", name,
                        )
            return [str(finding) for finding in findings]
        except Exception:
            log.exception("scheduler: pipeline boot checks failed to run")
            return []

    def _compute_catchup(self, now: datetime) -> list[str]:
        """Return enabled jobs whose most recent scheduled tick was missed.

        First boot (no runs.jsonl) → empty list. Only the most recent missed
        tick per job is replayed, so a week-long outage does not flood.
        """
        last_runs = load_last_runs(self.log_dir)
        catchup: list[str] = []
        for name, rt in self.registry.items():
            if not rt.enabled:
                continue
            if rt.cfg.when:
                # A trigger row has no missed tick to replay. Its condition is
                # a durable position, so whatever accumulated during the
                # downtime is still there and the first tick after boot fires
                # it — once, for the whole gap, rather than once per day of it.
                continue
            last = last_runs.get(name)
            if last is None:
                continue
            if rt.interval_seconds is not None:
                if (now - last).total_seconds() >= rt.interval_seconds:
                    catchup.append(name)
                continue
            try:
                # Cron is local-time per `_should_fire`. Convert the UTC `last`
                # to local naive, advance one cron step, convert back to UTC,
                # then compare with `now` (UTC). Naive .astimezone() in py3.6+
                # treats the value as system local.
                last_local = last.astimezone().replace(tzinfo=None)
                next_fire_local = croniter(rt.cfg.cadence, last_local).get_next(datetime)
                next_fire_utc = next_fire_local.astimezone(timezone.utc)
            except Exception:
                log.exception("scheduler: catch-up cron parse failed for %s", name)
                continue
            if next_fire_utc <= now:
                catchup.append(name)
        return catchup

    async def stop(self) -> None:
        self._stopping.set()
        for attr in ("_task", "_alarm_task"):
            task = getattr(self, attr)
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            setattr(self, attr, None)

        # Drain in-flight job tasks. Give each a short grace period to
        # finish cleanly; cancel anything still running so Mirror shutdown
        # cannot leave background writers alive after `stop()` returns.
        if self._inflight:
            pending = list(self._inflight)
            try:
                done, still_running = await asyncio.wait(
                    pending, timeout=self.stop_join_timeout
                )
            except Exception:
                log.exception("scheduler: wait for in-flight jobs raised")
                still_running = set(pending)
            for task in still_running:
                task.cancel()
            if still_running:
                await asyncio.gather(*still_running, return_exceptions=True)
        self._inflight.clear()
        log.info("scheduler stopped")

    def spawn_tracked_task(self, coro, *, name: str) -> asyncio.Task:
        """Spawn a job task and register it in `_inflight` so `stop()` can
        await or cancel it. Call this instead of `asyncio.create_task` for
        anything that should not outlive the engine.
        """
        task = asyncio.create_task(coro, name=name)
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)
        return task

    def runtime_state(self, name: str) -> dict[str, Any]:
        rt = self.registry.get(name)
        if rt is None:
            raise KeyError(name)
        return {
            "name": name,
            "cadence": rt.cfg.cadence,
            # What fires it, when it is not a clock. The Schedule view renders
            # `when_reason` where it renders a next-fire time for a cron row —
            # "3 of 25 new skill uses" is the same answer as "next at 23:00",
            # for a row whose next time cannot be computed.
            "when": rt.cfg.when,
            "when_config": rt.cfg.when_config,
            "when_reason": rt.when_reason,
            "enabled": rt.enabled,
            "circuit_broken": rt.consecutive_failures >= MAX_CONSECUTIVE_FAILURES,
            "consecutive_failures": rt.consecutive_failures,
            "last_fired_at": rt.last_fired_at.isoformat() if rt.last_fired_at else None,
            "last_trigger": rt.last_trigger,
            "last_result": None if rt.last_result is None else {
                "ok": rt.last_result.ok,
                "detail": rt.last_result.detail,
                "duration_ms": rt.last_result.duration_ms,
                "payload": rt.last_result.payload,
            },
            # Surfaced for the Mirror Schedule view: only LLM-using
            # handlers render a role dropdown. `effective_model_role` is
            # what actually gets passed to the handler at fire time —
            # operator override OR the class default.
            "uses_llm": getattr(rt.handler_cls, "uses_llm", False),
            "model_role": rt.cfg.model_role,
            "default_model_role": getattr(rt.handler_cls, "default_model_role", None),
            "effective_model_role": rt.cfg.model_role
                or getattr(rt.handler_cls, "default_model_role", None),
        }

    @property
    def configs(self) -> list[JobConfig]:
        return [rt.cfg for rt in self.registry.values()]

    def _check_tool_gate(self, handler: str, config: dict[str, Any]) -> None:
        """Refuse a tool-running row whose tool needs approval.

        Resolved against the live policy every time rather than remembered on
        the row: `permissions.yaml` is the authority and it can change after a
        row is armed. `ToolNotSchedulable` is a `ValueError`, so every caller
        that already reports a bad cadence reports this the same way.
        """
        from tesseract.scheduler import tool_gate

        registry, policy = tool_gate.runtime_from_app(self._app)
        # The context the row would actually run under, not a bare one. A tool
        # answering `check_permissions` is being asked about a specific call,
        # and asking it under different conditions from the ones it will meet
        # is how the gate and the runtime start giving different answers.
        tool_gate.check_job(
            handler,
            config,
            registry=registry,
            policy=policy,
            context=tool_gate.scheduled_context(app=self._app),
        )

    def _armable(self, cfg: JobConfig, refused: list[str] | None = None) -> bool:
        """Whether a row read off disk may come up armed.

        Shared by the two doors that read rows off disk rather than being
        handed one: `reload_jobs`, when the watcher sees `schedule.yaml`
        change, and `_regate_rows` at boot. Neither is a tool call, and
        `schedule.yaml` is not one of the four files closed to `file_write`,
        so a row gated when it was created can have its `tool` swapped
        underneath it and a row can be written enabled having passed through
        no door at all. The job refuses the call either way, so nothing
        ungated ever runs; what this fixes is the Schedule panel saying ON
        about a row that cannot.

        **It only ever lowers.** `enabled` also carries what the operator last
        set and what the breaker decided after a run, and neither is this
        function's to undo.

        A gate that cannot read the policy says nothing here, because turning
        good rows off over an unreadable policy would be the guard causing the
        outage. That is not the shipped boot order (`config/boot.yaml` builds
        `tool_registry` in `core` and the scheduler in `wiring`, in sequence);
        it is an engine built with no app, which is every test harness.
        """
        from tesseract.scheduler.tool_gate import ToolNotSchedulable

        if not cfg.enabled:
            return False
        try:
            self._check_tool_gate(cfg.handler, cfg.config)
        except ToolNotSchedulable as refusal:
            if refusal.unknown:
                return True
            log.warning(
                "scheduler.reload_jobs: %s stays off — %s", cfg.name, refusal.reason
            )
            # Named to the caller, not only to the log. A row the FIRE refuses
            # files a card and sends a message; a row a reload refuses fired
            # nothing, so without this it just goes quiet, which is the state
            # a schedule must never be in.
            if refused is not None:
                refused.append(cfg.name)
            return False
        return True

    def set_enabled(self, name: str, enabled: bool) -> None:
        rt = self.registry.get(name)
        if rt is None:
            raise KeyError(name)
        # Arming is where a row becomes able to act, so the tool gate answers
        # here as well as at creation. Turning one OFF is always allowed.
        if enabled:
            self._check_tool_gate(rt.cfg.handler, rt.cfg.config)
        rt.enabled = enabled
        # Re-enabling a tripped job also resets the breaker so it fires next tick.
        if enabled:
            rt.consecutive_failures = 0
        rt.cfg = rt.cfg.model_copy(update={"enabled": enabled})
        self._persist_job(name, {"enabled": enabled})

    def add_job_runtime(
        self,
        *,
        name: str,
        cadence: str,
        handler: str,
        summary: str = "",
        enabled: bool = True,
        on_failure: str = "log",
        retry_policy: RetryPolicy | None = None,
        config: dict[str, Any] | None = None,
        delivery: list[str] | None = None,
    ) -> JobConfig:
        """Register a new job at runtime.

        Validates: name uniqueness, handler dotted-path against
        `ALLOWED_HANDLER_PREFIXES`, cadence (interval shorthand or cron),
        `on_failure ∈ {log, alert, disable}`, and a `summary` that says what
        the row is for. Persists to `schedule.yaml` then arms in
        `self.registry`.

        **The summary is required here rather than at each door.** Two of the
        three ways a row comes into being — the `schedule_create` tool and the
        Mirror's create route — both land on this call, so requiring it once
        gives them one rule and one message. The third way is a hand-edited
        `home/config/schedule.yaml`, which the tracker REPORTS as a gap and
        never refuses: this repo owns two of the three doors, and refusing at
        the one it does not own would refuse to boot.
        """
        if name in self.registry:
            raise ValueError(f"job {name!r} already exists")
        if not any(handler.startswith(prefix) for prefix in ALLOWED_HANDLER_PREFIXES):
            raise ValueError(
                f"handler {handler!r} is outside the allowed prefixes "
                f"({', '.join(ALLOWED_HANDLER_PREFIXES)})"
            )
        if on_failure not in ("log", "alert", "disable"):
            raise ValueError(f"on_failure must be log/alert/disable, got {on_failure!r}")
        # Same rule and same message as `set_cadence`, because they are the
        # two doors onto one question.
        interval = self._validated_interval(cadence)
        # Resolve the handler before we persist — fail loudly here rather
        # than silently parking a placeholder job. It runs BEFORE the summary
        # check so the claim below is true: the mechanics are what make it a
        # job at all, and being told to write better prose about a job that
        # cannot import is two round trips where one would do.
        handler_cls = self._resolve_handler(handler)
        if handler_cls is _PlaceholderJob:
            raise ValueError(f"handler {handler!r} is not importable")
        # A row that runs a tool may only name one that already runs without
        # asking. `tool_gate` holds the reasoning; a row on any other handler
        # passes straight through.
        self._check_tool_gate(handler, config or {})
        # Last, so a row whose cadence or handler is wrong hears about that
        # first.
        if len(summary.strip()) < MIN_SUMMARY_CHARS:
            raise ValueError(
                f"job {name!r} needs a summary of at least {MIN_SUMMARY_CHARS} "
                "characters saying what it is for — it is the line you will read "
                "in WHAT-RUNS.md and in Managed system months from now, when "
                "the handler name no longer tells you anything"
            )
        # Build the typed config — pydantic raises ValidationError on
        # malformed inputs (e.g. retry_policy missing fields).
        retry = retry_policy if retry_policy is not None else RetryPolicy(max_retries=0, backoff_seconds=0)
        job_cfg = JobConfig(
            name=name,
            cadence=cadence,
            handler=handler,
            summary=summary.strip(),
            enabled=enabled,
            on_failure=on_failure,
            retry_policy=retry,
            config=config or {},
            delivery=delivery,
        )
        persist_job_add(self.config_dir, job_cfg)
        self.registry[name] = _JobRuntime(
            cfg=job_cfg,
            handler_cls=handler_cls,
            interval_seconds=interval,
            enabled=enabled,
        )
        log.info("scheduler.add_job_runtime: %s (cadence=%s handler=%s enabled=%s)",
                 name, cadence, handler, enabled)
        return job_cfg

    def remove_job_runtime(self, name: str) -> JobConfig:
        """Remove a registered job at runtime.

        Returns the removed JobConfig. Raises KeyError if the name is
        not registered. The `schedule.yaml` entry is also removed; if
        it's missing from disk (already trimmed by an external edit),
        the in-memory removal still succeeds and the disk error is
        logged but not raised.
        """
        rt = self.registry.get(name)
        if rt is None:
            raise KeyError(name)
        try:
            persist_job_remove(self.config_dir, name)
        except KeyError:
            # The yaml entry is already gone (out-of-band edit). The
            # in-memory removal is still safe — both states converge.
            log.warning(
                "scheduler.remove_job_runtime: %s already absent from schedule.yaml — "
                "removing from in-memory registry only", name,
            )
        # Other exceptions (I/O error, locked file) re-raise so the
        # caller gets a clear failure. Without re-raise the in-memory
        # state would diverge from disk and the job would resurrect on
        # the next restart — silently undoing the operator's removal.
        cfg = rt.cfg
        del self.registry[name]
        log.info("scheduler.remove_job_runtime: %s", name)
        return cfg

    def reload_jobs(self) -> dict[str, list[str]]:
        """Diff the on-disk `schedule.yaml` against the live
        registry and re-arm without restart.

        Returns a summary `{"added", "removed", "changed"}` of job names so
        the watcher can compose a meaningful toast. Job execution state
        (consecutive_failures, last_fired_at) is preserved for jobs whose
        cadence/handler haven't changed; jobs whose handler changed are
        reset (treated like a brand-new job).
        """
        cfg = load_schedule_config(self.config_dir)
        live_names = set(self.registry)
        fresh = {job.name: job for job in cfg.jobs}
        fresh_names = set(fresh)

        added: list[str] = []
        removed: list[str] = []
        changed: list[str] = []
        # Rows this reload turned off because the tool they name needs
        # approval. Carried out so a surface can say so.
        refused: list[str] = []

        for name in fresh_names - live_names:
            job_cfg = fresh[name]
            handler_cls = self._resolve_handler(job_cfg.handler)
            _validate_model_role(name, job_cfg.model_role, handler_cls)
            if job_cfg.when:
                check_trigger_row(name, job_cfg.when, job_cfg.when_config)
            self.registry[name] = _JobRuntime(
                cfg=job_cfg,
                handler_cls=handler_cls,
                interval_seconds=_parse_interval(job_cfg.cadence),
                enabled=self._armable(job_cfg, refused) and handler_cls is not _PlaceholderJob,
            )
            added.append(name)

        for name in live_names - fresh_names:
            del self.registry[name]
            removed.append(name)

        for name in live_names & fresh_names:
            rt = self.registry[name]
            new_cfg = fresh[name]
            cadence_changed = rt.cfg.cadence != new_cfg.cadence
            when_changed = (
                rt.cfg.when != new_cfg.when
                or rt.cfg.when_config != new_cfg.when_config
            )
            handler_changed = rt.cfg.handler != new_cfg.handler
            enabled_changed = rt.cfg.enabled != new_cfg.enabled
            on_failure_changed = rt.cfg.on_failure != new_cfg.on_failure
            retry_changed = rt.cfg.retry_policy != new_cfg.retry_policy
            inner_config_changed = rt.cfg.config != new_cfg.config
            model_role_changed = rt.cfg.model_role != new_cfg.model_role
            # Where a row reports is a thing about the row like any other. It
            # was left out when `delivery` was added, so editing it in the file
            # was acknowledged by the watcher and then ignored until a restart.
            delivery_changed = rt.cfg.delivery != new_cfg.delivery
            if not any((
                cadence_changed,
                when_changed,
                handler_changed,
                enabled_changed,
                on_failure_changed,
                retry_changed,
                inner_config_changed,
                model_role_changed,
                delivery_changed,
            )):
                continue
            if model_role_changed:
                _validate_model_role(name, new_cfg.model_role, rt.handler_cls)
            if when_changed and new_cfg.when:
                check_trigger_row(name, new_cfg.when, new_cfg.when_config)

            if handler_changed:
                handler_cls = self._resolve_handler(new_cfg.handler)
                self.registry[name] = _JobRuntime(
                    cfg=new_cfg,
                    handler_cls=handler_cls,
                    interval_seconds=_parse_interval(new_cfg.cadence),
                    enabled=self._armable(new_cfg, refused) and handler_cls is not _PlaceholderJob,
                )
            else:
                rt.cfg = new_cfg
                if cadence_changed:
                    rt.interval_seconds = _parse_interval(new_cfg.cadence)
                if enabled_changed:
                    rt.enabled = (
                        self._armable(new_cfg, refused)
                        and rt.handler_cls is not _PlaceholderJob
                    )
                    if rt.enabled:
                        rt.consecutive_failures = 0
                elif inner_config_changed and rt.enabled:
                    # The row kept its enabled flag and changed WHAT IT RUNS,
                    # which is the swap the gate has to see. Re-gating here can
                    # only turn a row off: `rt.enabled` also carries a decision
                    # this process made after a run (the breaker at
                    # `_apply_outcome`), and an edit to something else must not
                    # resurrect it.
                    rt.enabled = self._armable(new_cfg, refused)
            changed.append(name)

        log.info(
            "scheduler.reload_jobs: +%d -%d ~%d", len(added), len(removed), len(changed)
        )
        return {
            "added": added,
            "removed": removed,
            "changed": changed,
            "refused": refused,
        }

    def set_model_role(self, name: str, model_role: str | None) -> None:
        """Update the per-job LLM role override.

        Validates the role exists in `roles.yaml` (when non-None) and that
        the handler actually opts into LLM use (`uses_llm=True`). Persists
        via `_persist_job` round-trip and updates the in-memory cfg so the
        next fire picks it up — no restart needed.
        """
        rt = self.registry.get(name)
        if rt is None:
            raise KeyError(name)
        normalized = (model_role or "").strip() or None
        if normalized is not None and not getattr(rt.handler_cls, "uses_llm", False):
            raise ValueError(
                f"job {name!r} handler does not use an LLM — model_role override has no effect"
            )
        _validate_model_role(name, normalized, rt.handler_cls)
        rt.cfg = rt.cfg.model_copy(update={"model_role": normalized})
        self._persist_job(name, {"model_role": normalized})

    def set_summary(self, name: str, summary: str) -> None:
        """Correct what one of the operator's own rows says it is for.

        The tracker renders a row with no summary as a gap and asks for one —
        so there has to be a way to answer that is not "hand-edit the yaml".
        A shipped row is refused, because what the app's own work is for is the
        manifest's to say, not a data file's.

        Asked here for `set_cadence`'s reason: the write refuses it too, and
        `_persist_job` swallows that, so the caller was told the sentence had
        been changed while the yaml still held the app's.
        """
        rt = self.registry.get(name)
        if rt is None:
            raise KeyError(name)
        if name in system_rows(self.config_dir):
            raise ValueError(
                f"what {name!r} is for is set by the app, so it cannot be "
                "changed here. Rows you wrote yourself say what you tell them."
            )
        cleaned = summary.strip()
        if len(cleaned) < MIN_SUMMARY_CHARS:
            raise ValueError(
                f"job {name!r} needs a summary of at least {MIN_SUMMARY_CHARS} "
                "characters saying what it is for"
            )
        rt.cfg = rt.cfg.model_copy(update={"summary": cleaned})
        self._persist_job(name, {"summary": cleaned})

    def _validated_interval(self, cadence: str) -> int | None:
        """Seconds for an interval cadence, ``None`` for a cron one, raising
        on either kind the scheduler cannot actually run.

        **A cadence under the tick is refused rather than accepted and
        rounded.** The loop looks at every row once every ``tick_seconds`` and
        fires what is due, so a row asking for less is checked at the tick like
        everything else. Arming it anyway leaves a row that READS as thirty
        seconds in `schedule.yaml`, prints as thirty seconds in
        `WHAT-RUNS.md`, and fires once a minute, which nobody finds out unless
        they count. Measured 2026-08-26 on a live row.

        Both doors this repo owns come through here, so there is one rule and
        one message. A hand-edited `schedule.yaml` is not refused, for the
        same reason a missing `summary` is not: refusing a file we do not own
        would refuse to boot.
        """
        interval = _parse_interval(cadence)
        if interval is None:
            try:
                croniter(cadence, datetime.now(timezone.utc))
            except Exception as exc:
                raise ValueError(f"invalid cadence {cadence!r}: {exc}") from exc
            return None
        if interval < self.tick_seconds:
            raise ValueError(
                f"a cadence of {cadence!r} would still only fire once every "
                f"{self.tick_seconds} seconds, because that is how often the "
                f"scheduler looks at every row. Ask for {self.tick_seconds}s or "
                "longer, and say if you need it faster than that"
            )
        return interval

    def set_cadence(self, name: str, cadence: str) -> None:
        """Re-time a row, if this is a row whose timing the operator owns.

        The ownership question is asked HERE as well as inside
        `persist_job_update`, and that is not a second rule: both call
        `refuse_cadence`. It is asked here because `_persist_job` logs and
        swallows what the write raises, so a refusal reached from a command or
        a tool would otherwise be reported as a change that worked and would be
        live in memory until the next restart put it back.
        """
        rt = self.registry.get(name)
        if rt is None:
            raise KeyError(name)
        if rt.cfg.when:
            raise ValueError(
                f"job {name!r} fires on {rt.cfg.when!r}, not on a clock — giving it a "
                "cadence would leave it with two firing rules. Tune `when_config`, or "
                "disable it"
            )
        refusal = refuse_cadence(
            name, system_rows(self.config_dir).get(name), cadence
        )
        if refusal:
            raise ValueError(refusal)
        interval = self._validated_interval(cadence)
        rt.cfg = rt.cfg.model_copy(update={"cadence": cadence})
        rt.interval_seconds = interval
        self._persist_job(name, {"cadence": cadence})

    def _persist_job(self, name: str, updates: dict[str, Any]) -> None:
        """Round-trip `<config_dir>/schedule.yaml` with `updates` applied.

        Operator edits from the Mirror have to survive backend restart — the
        in-memory registry would otherwise drift back to the on-disk values
        on reboot. Logs and swallows errors so a locked yaml can't kill the
        mutating command path; KeyError (job deleted from yaml while engine
        ran) gets a louder message because it signals out-of-band tampering
        rather than a transient I/O failure.
        """
        try:
            persist_job_update(self.config_dir, name, updates)
        except KeyError:
            log.warning(
                "scheduler: %s missing from schedule.yaml — change %r is live in-memory "
                "but will be lost on restart (yaml was edited out-of-band)", name, updates,
            )
        except Exception:
            log.exception("scheduler: persist %s %r failed — change is live but not on disk", name, updates)

    def is_running(self, name: str) -> bool:
        """Whether a run of this job is in flight, by any door.

        Public because a caller that spawns `run_now` as a task cannot see the
        refusal it would raise: the exception lands in the task and is logged,
        not answered. Asking first is how the operator gets told.

        **It answers a question for a person and reserves nothing.** Every
        caller has an await between asking and acting, so the answer can be
        stale by the time it is used, and the guard is `_claim` below. Reading
        this as a gate is what left the trigger path open.

        Every path answers here because `_claim` is what records it. Two
        answers to `is this running` is the shape this panel exists to remove,
        and the panel's own `firing_now()` reads the activity registry, which
        has always covered both.
        """
        return name in self._running

    def _claim(self, name: str) -> bool:
        """Take the in-flight seat for `name`, or report that it is taken.

        **The check and the claim are one act, and it is synchronous.** Asking
        first and starting later is not a guard: `_tick` decided a row was free
        while building `armed`, then `_tick_triggers` awaited every condition
        before firing, and a run started by hand inside that await was invisible
        to a decision already made. Measured at two concurrent runs of one job.

        So every door claims here, at the moment it commits, rather than inside
        the coroutine that eventually runs. `_run_job` only releases.
        """
        if name in self._running:
            return False
        self._running.add(name)
        return True

    async def run_now(self, name: str, *, trigger: str) -> JobResult:
        """Fire a registered job immediately, off-schedule.

        Same execution path as the tick loop (`_run_job` handles run-id,
        broadcast envelopes, retries, breaker bookkeeping, `runs.jsonl` write).
        Raises KeyError if `name` is not registered. The job's `enabled` flag
        is ignored — manual triggers work even on disabled rows, which is the
        whole point of "run now".

        `trigger` has no default on purpose. Both callers fire by hand and they
        are not the same event — `operator` is a human at the Run-now button,
        `assistant` is the `schedule_run` tool — and a default would quietly
        merge them back into the single word this field exists to split.
        """
        rt = self.registry.get(name)
        if rt is None:
            raise KeyError(name)
        # One run of a job at a time. Refused here rather than left to
        # `_run_job`, because the caller asked for a run and deserves to be
        # told it is not getting one; `_run_job` is also the tick's path, and
        # a tick skips quietly rather than raising.
        if not self._claim(name):
            raise AlreadyRunning(name)
        fired_at = datetime.now(timezone.utc)
        rt.last_fired_at = fired_at
        return await self._run_job(name, rt, fired_at, trigger=trigger)

    async def _tick_loop(self) -> None:
        try:
            while not self._stopping.is_set():
                # Atomic snapshot: a single `datetime.now()` call yields both
                # a tz-aware UTC for storage/dedupe and a naive local for cron
                # matching. Two separate now() calls would race the minute
                # boundary and could let `croniter.match` see a different
                # minute than the 60-second dedupe.
                now_local_aware = datetime.now().astimezone()
                now_utc = now_local_aware.astimezone(timezone.utc)
                now_local = now_local_aware.replace(tzinfo=None)
                await self._tick(now_utc, now_local)
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self.tick_seconds)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduler tick loop crashed")
            raise

    async def _alarm_tick_loop(self) -> None:
        try:
            while not self._stopping.is_set():
                app = self._app
                registry = app.get("alarm_registry") if (app is not None and hasattr(app, "get")) else None
                if registry is not None:
                    await registry.tick(app, datetime.now(timezone.utc))
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self.alarm_tick_seconds)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduler alarm loop crashed")
            raise

    async def _tick(self, now_utc: datetime, now_local: datetime) -> None:
        armed: list[tuple[str, _JobRuntime]] = []
        for name, rt in self.registry.items():
            if not rt.enabled or name in self._catchup_pending:
                continue
            if name in self._running:
                # A run started by hand is still going. Firing the same job on
                # top of it is two passes over the same files, and the clock
                # coming round is the weakest of the reasons to want that.
                log.info("scheduler: %s is still running, so this tick skips it", name)
                continue
            if rt.cfg.when:
                armed.append((name, rt))
                continue
            if not self._should_fire(rt, now_utc, now_local):
                continue
            if not self._claim(name):
                log.info("scheduler: %s is still running, so this tick skips it", name)
                continue
            rt.last_fired_at = now_utc
            self.spawn_tracked_task(
                self._run_job(name, rt, now_utc, trigger="scheduled"),
                name=f"scheduler-{name}",
            )
        if armed:
            await self._tick_triggers(armed, now_utc)

    async def _tick_triggers(
        self, armed: list[tuple[str, _JobRuntime]], now_utc: datetime
    ) -> None:
        """Ask every armed condition at once, then fire the ones that said yes.

        Concurrent because each condition reads a file: asked in sequence, a
        slow disk on the first would delay the answer for every row behind it,
        on a tick that also owes the cron rows their minute. `evaluate` never
        raises, so nothing here needs `return_exceptions`.
        """
        verdicts = await asyncio.gather(
            *(
                evaluate_trigger(
                    name,
                    rt.cfg.when,
                    rt.cfg.when_config,
                    now=now_utc,
                    fired_this_process=rt.fired_this_process,
                    watermarks=self._watermark_store(),
                )
                for name, rt in armed
            )
        )
        for (name, rt), verdict in zip(armed, verdicts):
            rt.when_reason = verdict.reason
            if not verdict.fire:
                continue
            # Position first, then dispatch. The reverse order re-fires on the
            # next tick if the run outlives it, and for a row that calls a
            # model that is a second bill for one event.
            # Asked again here, not only in `_tick`. Every condition above was
            # awaited, and a run started by hand inside that await would not be
            # in the answer this loop was handed.
            if not self._claim(name):
                log.info("scheduler: %s is still running, so this tick skips it", name)
                continue
            record_trigger_fired(name, now_utc, self._watermark_store())
            rt.fired_this_process = True
            rt.last_fired_at = now_utc
            log.info("scheduler: %s fired on %s — %s", name, rt.cfg.when, verdict.reason)
            self.spawn_tracked_task(
                self._run_job(name, rt, now_utc, trigger="event"),
                name=f"scheduler-{name}",
            )

    @staticmethod
    def _should_fire(rt: _JobRuntime, now_utc: datetime, now_local: datetime) -> bool:
        # Cron expressions in schedule.yaml are interpreted in **system local
        # time** so the operator can write `30 22 * * *` and have it mean
        # 22:30 wall-clock regardless of what zone the host runs in. Storage
        # (`last_fired_at`, `runs.jsonl`) stays UTC for portability.
        if rt.interval_seconds is not None:
            if rt.last_fired_at is None:
                return True
            return (now_utc - rt.last_fired_at) >= timedelta(seconds=rt.interval_seconds)
        try:
            if not croniter.match(rt.cfg.cadence, now_local):
                return False
        except Exception:
            log.exception("scheduler: cron match failed for %s (%r)", rt.cfg.name, rt.cfg.cadence)
            return False
        # audit-1 m5 (2026-04-24): 60s in-slot dedupe. `croniter.match` is
        # minute-granular, so without this a `run_now` + a `_tick` in the
        # same wall-clock minute both fire the job. Also guards against
        # early timer re-entry. Compared in UTC since `last_fired_at` is UTC.
        if rt.last_fired_at is not None and (now_utc - rt.last_fired_at) < timedelta(seconds=60):
            return False
        return True

    async def _run_job(
        self,
        name: str,
        rt: _JobRuntime,
        fired_at: datetime,
        trigger: str = "scheduled",
    ) -> JobResult:
        """Run a job with retry + circuit-breaker bookkeeping. Never raises.

        **Whatever happens here, the seat is freed.** The caller claimed it
        with `_claim` before committing, so the `finally` covers this whole
        function and not only the attempt loop: a bad trigger name or a handler
        whose constructor throws would otherwise leave a job marked as running
        for the life of the process, and every later run of it refused. A
        refusal that outlives one bad run is worse than the defect the seat
        exists to prevent.
        """
        try:
            if trigger not in TRIGGER_SOURCES:
                raise ValueError(
                    f"unknown trigger {trigger!r} for job {name!r}: expected one of "
                    f"{', '.join(sorted(TRIGGER_SOURCES))}"
                )
            handler = rt.handler_cls()
            attempts = rt.cfg.retry_policy.max_retries + 1
            backoff = rt.cfg.retry_policy.backoff_seconds
            result = JobResult(job_name=name, run_id="", ok=False, detail="no attempts")
            run_id = uuid.uuid4().hex
            # The seat is already held by whichever door committed to this run.
            # Held again here so a direct call in a test behaves like the real
            # paths, and because `add` on a set that has it changes nothing.
            # Registered BEFORE the envelope: the broadcast awaits a socket
            # write, which yields the loop, and a surface that re-reads on that
            # envelope would ask what is running before this had said anything.
            self._running.add(name)
            register_routine(run_id, label=name)
            await _broadcast_envelope(self._app, "schedule_job_started", {
                "job_name": name,
                "run_id": run_id,
                "fired_at": fired_at.isoformat(),
            })
            return await self._attempt_job(
                name, rt, fired_at, trigger, handler, attempts, backoff, result, run_id
            )
        finally:
            self._running.discard(name)

    async def _attempt_job(
        self,
        name: str,
        rt: _JobRuntime,
        fired_at: datetime,
        trigger: str,
        handler: BaseJob,
        attempts: int,
        backoff: int,
        result: JobResult,
        run_id: str,
    ) -> JobResult:
        """The retry loop and its bookkeeping. Split out so `_run_job` can hold
        the in-flight seat around the whole of it in one `finally`, rather than
        threading one through every return the loop already has."""
        for attempt in range(attempts):
            ctx = JobContext(
                job_name=name,
                run_id=run_id,
                fired_at=fired_at,
                app=self._app,
                config=dict(rt.cfg.config),
                log_dir=self.log_dir,
                model_role=rt.cfg.model_role,
                # A row named after its manifest entry bills to itself. A
                # handler that runs under many operator-chosen names says which
                # entry it belongs to, so its ceiling binds instead of the row
                # spending under a name nothing declares a cap for.
                billing_key=type(handler).billing_entry or name,
                delivery=(
                    None if rt.cfg.delivery is None else tuple(rt.cfg.delivery)
                ),
                cost_ledger=(
                    self._app.get("cost_ledger") if self._app is not None else None
                ),
                trigger_source=trigger,
            )
            t0 = time.perf_counter()
            try:
                result = await handler.run(ctx)
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                result = dataclasses.replace(
                    result,
                    duration_ms=result.duration_ms or elapsed_ms,
                )
            except Exception as exc:
                result = JobResult(
                    job_name=name,
                    run_id=ctx.run_id,
                    ok=False,
                    detail=f"unhandled exception: {exc!r}",
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
                log.exception("scheduler: %s raised", name)
            if trigger == "catchup":
                result = dataclasses.replace(result, payload={**result.payload, "catchup": True})
            # Off the loop, and not for the write's own sake. `append_run_log`
            # takes the module lock that `prune_older_than` holds for the whole
            # of a rewrite — a `threading.Lock` with no await point, so a job
            # completing while the nightly retention sweep runs would block the
            # event loop until the sweep finished. This is the precondition the
            # approval ledger already meets (`approval_log.py` appends via
            # `to_thread` for exactly this reason) and the run log did not.
            await asyncio.to_thread(
                append_run_log, ctx, result, log_dir=self.log_dir
            )
            if result.ok:
                break
            if attempt < attempts - 1 and backoff > 0:
                await asyncio.sleep(backoff)

        rt.last_result = result
        rt.last_trigger = trigger
        self._apply_outcome(rt, result)
        circuit_broken = rt.consecutive_failures >= MAX_CONSECUTIVE_FAILURES
        done_payload: dict[str, Any] = {
            "job_name": name,
            "run_id": run_id,
            "fired_at": fired_at.isoformat(),
            "ok": result.ok,
            "detail": result.detail,
            "payload": dict(result.payload),
            "duration_ms": result.duration_ms,
            "circuit_broken": circuit_broken,
            "trigger_source": trigger,
        }
        await _broadcast_envelope(self._app, "schedule_job_done", done_payload)
        if (not result.ok) and rt.cfg.on_failure == "alert":
            alert = {
                "job_name": name,
                "run_id": run_id,
                "ok": False,
                "detail": result.detail,
                "consecutive_failures": rt.consecutive_failures,
                "circuit_broken": circuit_broken,
            }
            # The panel and the channel, not one or the other. The envelope is
            # how a row's failure reaches a screen that is already open; the
            # notification is how it reaches an operator who is not at it, and
            # routing can take that one away without taking the row off the
            # panel. Neither waits on the other: one is a socket write on this
            # machine and the other is an HTTP call to a channel, and both
            # swallow their own failures.
            await asyncio.gather(
                _broadcast_envelope(self._app, "schedule_job_failed", alert),
                self._announce_failure(rt, alert),
            )
        # Operator must not lose a failed run to a silent chip disappearance
        # (2026-07-05) — a successful run's chip is removed as before, a
        # failed run's chip transitions to ``failed`` and stays until the
        # operator dismisses it via the activity map's close button.
        if result.ok:
            remove_routine(run_id)
        else:
            fail_routine(run_id, detail=result.detail)
        return result

    async def _announce_failure(self, rt: _JobRuntime, alert: dict[str, Any]) -> None:
        """Tell the operator a row they asked about failed.

        Where it goes is the row's `delivery` if it stated one and
        `routing.yaml::schedule_failed` otherwise. A notifier that is absent
        (a test, a boot with no channels) is not a failure of the job: the run
        log and the envelope both already have this.
        """
        app = self._app
        notifier = app.get("outbound_notifier") if hasattr(app, "get") else None
        if notifier is None:
            return
        try:
            await notifier.notify(
                "schedule_failed", alert, destinations=rt.cfg.delivery,
            )
        except Exception:
            log.exception("scheduler: could not send the failure of %s", rt.cfg.name)

    def _apply_outcome(self, rt: _JobRuntime, result: JobResult) -> None:
        if result.ok:
            rt.consecutive_failures = 0
            return
        rt.consecutive_failures += 1
        if rt.cfg.on_failure == "alert":
            self.pending_alerts.append(result)
        if rt.cfg.on_failure == "disable":
            rt.enabled = False
            log.warning("scheduler: %s disabled via on_failure=disable", rt.cfg.name)
            return
        if rt.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            rt.enabled = False
            log.warning("scheduler: %s disabled after %d consecutive failures", rt.cfg.name, rt.consecutive_failures)


class _PlaceholderJob(BaseJob):
    """Stand-in when a handler import fails at init. Keeps the runtime alive."""

    async def run(self, ctx: JobContext) -> JobResult:
        return JobResult(
            job_name=ctx.job_name,
            run_id=ctx.run_id,
            ok=False,
            detail="handler module not importable (placeholder)",
        )


_MIRROR_BROADCAST: tuple[Any, Any] | None = None
_MIRROR_BROADCAST_FAILED = False


def _load_mirror_broadcast() -> tuple[Any, Any] | None:
    """Resolve + cache Mirror envelope/session helpers. Returns None once
    (and stays None) if the Mirror package isn't importable — prevents the
    one-import-per-tick noise amplifier — audit-1 m10 (2026-04-24).
    """
    global _MIRROR_BROADCAST, _MIRROR_BROADCAST_FAILED
    if _MIRROR_BROADCAST is not None:
        return _MIRROR_BROADCAST
    if _MIRROR_BROADCAST_FAILED:
        return None
    try:
        from tesseract.mirror.server.envelope import make_envelope
        from tesseract.mirror.server.session import send_envelope
    except Exception:
        log.exception("scheduler broadcast: mirror envelope/session import failed; suppressing further attempts")
        _MIRROR_BROADCAST_FAILED = True
        return None
    _MIRROR_BROADCAST = (make_envelope, send_envelope)
    return _MIRROR_BROADCAST


async def _broadcast_envelope(app: Any, envelope_type: str, data: dict[str, Any]) -> None:
    """Fan `envelope_type` out to every connected Mirror WS. Category=`schedule`.

    Never raises — scheduler must not fail a job because a WS pipe went away.
    Runs outside any Mirror session, so `session_id` is empty.
    """
    if app is None or not hasattr(app, "get"):
        return
    sessions = app.get("server_sessions") or {}
    if not sessions:
        return
    helpers = _load_mirror_broadcast()
    if helpers is None:
        return
    make_envelope, send_envelope = helpers
    for sess in list(sessions.values()):
        env = make_envelope(envelope_type, "schedule", getattr(sess, "session_id", ""), data)
        try:
            await send_envelope(sess, env)
        except Exception:
            log.exception("scheduler broadcast: send_envelope failed for %s", getattr(sess, "session_id", "?"))
