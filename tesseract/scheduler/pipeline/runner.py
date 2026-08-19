"""The ordered executor.

Small on purpose: the stage bodies are the existing job bodies, wrapped. What
this adds is what cron cannot — an order that is declared rather than encoded
in clock minutes, a manifest that survives a hard kill, and one run per gap
instead of one per missed tick.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tesseract.orchestrator.outcome import RunOutcome
from tesseract.scheduler.pipeline.artifacts import ArtifactStore, WatermarkStore
from tesseract.scheduler.pipeline.graph import execution_order, upstreams
from tesseract.scheduler.pipeline.manifest import (
    ManifestStore,
    MemoryManifestStore,
    RunManifest,
    StageRow,
)
from tesseract.scheduler.pipeline.stage import (
    ProviderUnreachable,
    Stage,
    StageCadence,
    StageContext,
    StageReport,
)

log = logging.getLogger(__name__)

# What each cadence word means, and how much earlier than that a run may
# start and still count. The anchor drifts by minutes between nights (a boot
# delay, a long stage ahead of this one); without the slack a run that starts
# ten minutes early leaves every daily stage not-due and the night does
# nothing at all.
_CADENCE_PERIOD: dict[StageCadence, timedelta] = {
    StageCadence.CONTINUOUS: timedelta(0),
    StageCadence.DAILY: timedelta(days=1),
    StageCadence.WEEKLY: timedelta(days=7),
}
_DUE_SLACK = timedelta(hours=4)

# How stale an unfinished run may be and still be worth resuming. Past this it
# describes a night that is over: its window ended then, and continuing it
# would record coverage nobody achieved.
_RESUME_MAX_AGE = timedelta(days=1)

# An upstream in one of these states means its consumer never ran. `degraded`
# and `truncated` are deliberately absent: partial input is still input, and a
# consumer that cannot use it says so in its own outcome.
_BLOCKING_OUTCOMES = frozenset(
    {RunOutcome.FAILED, RunOutcome.SKIPPED_UPSTREAM_FAILED}
)


class PipelineRunner:
    """Runs a declared set of stages in order, once."""

    def __init__(
        self,
        stages: Sequence[Stage],
        *,
        manifests: ManifestStore | MemoryManifestStore | None = None,
        artifacts: ArtifactStore | None = None,
        watermarks: WatermarkStore | None = None,
        app: Any = None,
        config: dict[str, Any] | None = None,
        log_dir: Path | None = None,
        external_reads: frozenset[str] = frozenset(),
        entry: str = "",
    ) -> None:
        # Raises on an unsatisfied read or a cycle. Deliberately at
        # construction: a graph that cannot be ordered has no safe partial run.
        self._stages = execution_order(stages, external_reads=external_reads)
        self._upstreams = upstreams(stages)
        self._manifests = manifests or ManifestStore()
        self._artifacts = artifacts or ArtifactStore()
        self._watermarks = watermarks or WatermarkStore()
        self._app = app
        # The row's `config:` block, keyed by stage name. One row now carries
        # what were several rows' blocks, so a stage reads its own sub-block
        # rather than a flattened union nobody could attribute.
        self._config = dict(config or {})
        self._log_dir = log_dir
        # The manifest entry these stages belong to, handed down so a stage
        # that names a chain has something to bill. Empty outside a row (a
        # single-stage run from the CLI), where the stage's own name is the
        # only honest answer.
        self._entry = entry

    @property
    def stages(self) -> tuple[Stage, ...]:
        return self._stages

    def is_due(self, stage: Stage, anchor: datetime) -> bool:
        """Whether `stage` has come round again by the time of `anchor`.

        Driven by the stage's own watermark, never by how many times a clock
        fired — which is what makes a week of downtime one run rather than
        seven.
        """
        last = self._watermarks.get(stage.name)
        if last is None:
            return True
        return (anchor - last) >= (_CADENCE_PERIOD[stage.cadence] - _DUE_SLACK)

    async def run(self, *, anchor: datetime | None = None, resume: bool = True) -> RunManifest:
        """One pipeline run, resuming an unfinished one if there is one."""
        when = anchor or datetime.now(timezone.utc)
        open_manifest = self._manifests.load_open() if resume else None
        if open_manifest is not None and (when - open_manifest.anchor) > _RESUME_MAX_AGE:
            # An interrupted run that nobody restarted for days is history, not
            # a run in progress. Resuming it would hand the survivors a window
            # ending in the past and record a position they never reached, so
            # it is closed as it stands — the record survives under runs/ —
            # and a fresh run starts at the real anchor.
            log.info(
                "pipeline: abandoning stale run %s (anchor %s), starting fresh",
                open_manifest.run_id, open_manifest.anchor.isoformat(),
            )
            self._manifests.finish(open_manifest)
            open_manifest = None
        if open_manifest is not None:
            # The anchor stays the one the interrupted run began with: its
            # stages were ordered against that window, and moving it would
            # hand the survivors a different one from their predecessors.
            manifest = open_manifest
            log.info(
                "pipeline: resuming run %s at stage %d of %d",
                manifest.run_id,
                len(manifest.rows) + 1,
                len(self._stages),
            )
        else:
            manifest = RunManifest(
                run_id=uuid.uuid4().hex,
                anchor=when,
                started_at=datetime.now(timezone.utc),
            )
            self._manifests.commit(manifest)

        for stage in self._stages:
            if stage.name in manifest.committed:
                continue
            if not self.is_due(stage, manifest.anchor):
                if stage.name not in manifest.not_due:
                    manifest.not_due.append(stage.name)
                    self._manifests.commit(manifest)
                continue
            if not self._enabled(stage):
                # Recorded, not run, and NOT a `refused` row: worst-outcome
                # wins for the row, so a stage the operator turned off once
                # would otherwise make every night read `refused` forever.
                if stage.name not in manifest.disabled:
                    manifest.disabled.append(stage.name)
                    self._manifests.commit(manifest)
                continue
            blocker = self._blocked_by(stage, manifest)
            if blocker is not None:
                row = StageRow(
                    stage=stage.name,
                    outcome=RunOutcome.SKIPPED_UPSTREAM_FAILED,
                    reason=f"{blocker} did not succeed, and this stage reads what it writes",
                )
            else:
                row = await self._execute(stage, manifest.run_id, manifest.anchor)
            manifest.rows.append(row)
            # Committed after EVERY stage: this is what a kill mid-run costs.
            self._manifests.commit(manifest)

        self._manifests.finish(manifest)
        return manifest

    async def run_one(self, name: str, *, anchor: datetime | None = None) -> StageRow:
        """Run one stage by name, without the schedule and without its graph.

        The operator's `--stage memory_lint`. Cadence is not consulted: asking
        for a stage by name IS the reason to run it.
        """
        stage = next((s for s in self._stages if s.name == name), None)
        if stage is None:
            raise KeyError(f"no stage named {name!r}")
        when = anchor or datetime.now(timezone.utc)
        run_id = f"stage-{uuid.uuid4().hex}"
        row = await self._execute(stage, run_id, when)
        manifest = RunManifest(
            run_id=run_id,
            anchor=when,
            started_at=datetime.now(timezone.utc),
            rows=[row],
        )
        self._manifests.finish(manifest)
        return row

    def _enabled(self, stage: Stage) -> bool:
        """Per-stage `enabled:`, which is what a collapsed row must not cost.

        Eighteen `schedule.yaml` rows each carried an `enabled` flag the
        operator could flip. Two rows carry two. The flag moves onto the
        stage's own config block rather than disappearing, and a stage turned
        off reports `refused` — a decision, which is exactly what it is.
        """
        block = self._config.get(stage.name)
        if not isinstance(block, dict):
            return True
        return bool(block.get("enabled", True))

    def _blocked_by(self, stage: Stage, manifest: RunManifest) -> str | None:
        for upstream in sorted(self._upstreams.get(stage.name, ())):
            if manifest.outcome_of(upstream) in _BLOCKING_OUTCOMES:
                return upstream
        return None

    async def _execute(self, stage: Stage, run_id: str, anchor: datetime) -> StageRow:
        window_start = self._watermarks.get(stage.name)
        ctx = StageContext(
            stage=stage,
            run_id=run_id,
            anchor=anchor,
            window_start=window_start,
            window_end=anchor,
            artifacts=self._artifacts,
            app=self._app,
            config=dict(self._config.get(stage.name) or {}),
            log_dir=self._log_dir,
            entry=self._entry,
        )
        started = datetime.now(timezone.utc)
        t0 = time.perf_counter()
        # A stage may declare retries, and one migrated stage needs them: the
        # `index_rebuild` row carried max_retries=1 and is deterministic, so
        # the "a retry re-bills a model call" argument that justified dropping
        # the model stages' retries does not cover it. Only a `failed` attempt
        # is retried — a refusal or a truncation would repeat on the retry.
        for attempt in range(stage.retries + 1):
            if attempt:
                log.info(
                    "pipeline: retrying %s (attempt %d of %d)",
                    stage.name, attempt + 1, stage.retries + 1,
                )
                await asyncio.sleep(stage.retry_backoff_seconds)
                ctx.reads_seen.clear()
                ctx.writes_made.clear()
            try:
                report = await asyncio.wait_for(
                    stage.body(ctx), timeout=stage.budget_seconds
                )
            except asyncio.TimeoutError:
                report = StageReport(
                    outcome=RunOutcome.TRUNCATED,
                    reason=(
                        f"stopped at its {stage.budget_seconds:g}s budget; the rest "
                        "resumes from the same watermark next run"
                    ),
                )
            except ProviderUnreachable as exc:
                report = StageReport(
                    outcome=RunOutcome.DEGRADED,
                    reason=f"provider unreachable: {exc}",
                )
            except Exception as exc:  # a stage body must not take the run down
                log.exception("pipeline: stage %s raised", stage.name)
                report = StageReport(
                    outcome=RunOutcome.FAILED,
                    reason=f"unhandled exception: {exc!r}",
                )
            if report.outcome is not RunOutcome.FAILED:
                break
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        if report.watermark is not None:
            # A stage that names its own position has measured it — a bounded
            # catch-up that covered three of thirty days knows exactly where it
            # stopped, and discarding that would make it redo those three
            # forever. An explicit mark is always honoured, whatever the
            # outcome.
            self._watermarks.set(stage.name, report.watermark)
        elif report.outcome in (RunOutcome.SUCCEEDED, RunOutcome.SKIPPED_NO_WORK):
            # Otherwise only a run that finished its window may move the mark.
            # A degraded or truncated stage covered part of it, and moving past
            # the part it missed is how work disappears silently.
            self._watermarks.set(stage.name, ctx.window_end)

        return StageRow(
            stage=stage.name,
            outcome=report.outcome,
            reason=report.reason,
            changed=report.changed,
            refused=report.refused,
            reads=dict(ctx.reads_seen),
            writes=dict(ctx.writes_made),
            started_at=started,
            ended_at=datetime.now(timezone.utc),
            duration_ms=elapsed_ms,
        )


__all__ = ["PipelineRunner"]
