"""The consolidate row — settling the day into memory, in a declared order.

Sixteen `schedule.yaml` rows fired between 20:00 and 04:00 to do one thing, and
the order between them was whatever the clock happened to impose. Ten of them
sat between 20:00 and 23:45; `index_rebuild` sat at 15:30, hours BEFORE every
job that writes what it indexes, so the indexes were a day behind every night
and nothing said so.

Two things are worth being precise about in here, because they are the
difference between a pipeline and a rename:

**Most of these stages do not consume each other.** They read canonical files —
the session store, the daily layer, the recall log — so declaring `reads`
between them would invent data dependencies that do not exist, and with them a
failure cascade that would skip the night's deterministic maintenance because a
model was unreachable. Those relations are `after`: run later, do not wait on
the result. The one genuine data edge in the whole set is the one this plan
started from — `memory_lint` produces findings and `memory_scrub` consumes
them, which is why scrub is the only stage that can be skipped for upstream
failure.

**Two lints report `ok=False` when they FIND something**, because
`on_failure: alert` was the only channel they had to the operator. Read
literally, a lint that found work would fail and scrub would never run. Both
declare their own reading here, and the findings ride the reason instead.

A third reading problem arrives with every job that moves in from its own row:
a job written for cron says whether it RAISED, and a stage has to say what came
of it. `provider_probe` returns `ok=True` with a dead API key in the payload;
`agenda_reap` returns `ok=True` with an empty sweep when its store would not
open. Neither is a clean night, and neither said so while it was a row.
"""

from __future__ import annotations

from tesseract.orchestrator.outcome import RunOutcome
from tesseract.scheduler.pipeline.job_stage import counted, job_stage, payload_counts
from tesseract.scheduler.pipeline.registry import Row, register_row
from tesseract.scheduler.pipeline.stage import StageCadence, StageKind, StageReport
from tesseract.scheduler.tasks.agenda_reaper import AgendaReaperJob
from tesseract.scheduler.tasks.atlas_build import AtlasBuildJob
from tesseract.scheduler.tasks.atlas_verify import AtlasVerifyJob
from tesseract.scheduler.tasks.brief_render import BriefRenderJob
from tesseract.scheduler.tasks.chat_digest import ChatDigestJob
from tesseract.scheduler.tasks.conscience_heartbeat import ConscienceHeartbeatJob
from tesseract.scheduler.tasks.daily_writer import DailyWriterJob
from tesseract.scheduler.tasks.dream_cycle import DreamCycleJob
from tesseract.scheduler.tasks.feedback_consolidator import FeedbackConsolidatorJob
from tesseract.scheduler.tasks.feedback_sweep import FeedbackSweepJob
from tesseract.scheduler.tasks.index_rebuild import IndexRebuildJob
from tesseract.scheduler.tasks.interests_decay import InterestsDecayJob
from tesseract.scheduler.tasks.leaf_digest_daily import DigestDailyJob
from tesseract.scheduler.tasks.leaf_topic_route import TopicRouteJob
from tesseract.scheduler.tasks.librarian_heartbeat import LibrarianHeartbeatJob
from tesseract.scheduler.tasks.memory_lint import MemoryLintJob
from tesseract.scheduler.tasks.memory_relink import MemoryRelinkJob
from tesseract.scheduler.tasks.memory_scrub import MemoryScrubJob
from tesseract.scheduler.tasks.provider_probe import ProviderProbeJob
from tesseract.scheduler.tasks.retention import RetentionJob
from tesseract.scheduler.tasks.vault_lint import VaultLintJob
from tesseract.scheduler.tasks.vault_raw_watch import VaultRawWatchJob
from tesseract.scheduler.tasks.work_index_sweep import WorkIndexSweepJob
from tesseract.scheduler.types import JobResult

ROW_NAME = "consolidate"

# One model turn each, and none of them is new: these four were already the
# only jobs in the nightly set that call a provider, at the same once-a-night
# cadence they have now.
_MODEL_BUDGET = 600.0

# How many missed days a model stage walks in one night. A gap has to be
# covered or the work is lost, but a month away must not become a month of
# provider calls in one pass — beyond this the stage reports `truncated` and
# the next run picks up where it stopped.
_MODEL_CATCHUP_DAYS = 3


def _memory_lint_report(result: JobResult) -> StageReport:
    """`ok=False` means "the store has findings", which is the lint working.

    A real failure — no memory bundle, no store directory, a crash — carries no
    report in its payload, and that is what tells the two apart.
    """
    report = result.payload
    if "broken_wikilinks" not in report:
        return counted(
            RunOutcome.FAILED,
            result.detail or "the linter could not read the memory store",
        )
    total = sum(
        len(report.get(key) or ())
        for key in (
            "broken_wikilinks",
            "broken_frontmatter_links",
            "stale_source_paths",
            "orphan_stubs",
        )
    )
    return StageReport(
        outcome=RunOutcome.SUCCEEDED,
        reason=f"{total} finding(s): {result.detail}" if total else "",
    )


def _memory_scrub_report(result: JobResult) -> StageReport:
    payload = result.payload
    if payload.get("mode") == "report":
        return counted(
            RunOutcome.SUCCEEDED,
            f"report mode: {payload.get('scrubbable', 0)} finding(s) are repairable",
        )
    after = payload.get("report_after") or {}
    if not after:
        return counted(
            RunOutcome.FAILED, result.detail or "the scrub could not read the store"
        )
    fixed = int(payload.get("fixed_frontmatter_links") or 0) + int(
        payload.get("fixed_orphan_stubs") or 0
    )
    # What it deliberately did not touch: a source path that rotted needs the
    # file moved back, and a wikilink into a missing path needs a decision.
    # Both are operator work, and counting them as refusals is the whole point
    # of the second number.
    refused = len(after.get("stale_source_paths") or ()) + len(
        after.get("broken_wikilinks") or ()
    )
    if not result.ok:
        return counted(
            RunOutcome.DEGRADED,
            f"{result.detail} — repairable findings survived the pass",
            changed=fixed,
            refused=refused,
        )
    if not fixed:
        return counted(
            RunOutcome.SKIPPED_NO_WORK,
            "nothing was broken",
            refused=refused,
        )
    return counted(RunOutcome.SUCCEEDED, changed=fixed, refused=refused)


def _provider_probe_report(result: JobResult) -> StageReport:
    """A drifting provider is a finding, not a failure — and not a clean night.

    The job returns `ok=True` even when probes come back bad, deliberately: it
    reached every role and recorded telemetry, and an `ok=False` would have the
    engine retry and re-bill the probes. But `succeeded` would put a dead API
    key on the same line as a healthy sweep, so drift reads `degraded` — which
    keeps the run OK and therefore does not fire the row's `on_failure: alert`,
    while still saying on the manifest that this night found something.
    """
    payload = result.payload if isinstance(result.payload, dict) else {}
    if not result.ok or "probed" not in payload:
        return counted(
            RunOutcome.FAILED, result.detail or "the probe sweep did not run"
        )
    probed = int(payload.get("probed") or 0)
    failures = len(payload.get("failures") or ())
    # A role whose kind has no probe. Not a failure of the provider — a gap in
    # the roster — and the second number is where a gap belongs.
    skipped = len(payload.get("skipped") or ())
    if not probed:
        return counted(RunOutcome.SKIPPED_NO_WORK, "no active role to probe")
    if failures:
        return counted(
            RunOutcome.DEGRADED,
            f"{failures} of {probed} role(s) drifted: {result.detail}",
            changed=probed,
            refused=failures + skipped,
        )
    return counted(
        RunOutcome.SUCCEEDED, result.detail, changed=probed, refused=skipped
    )


_AGENDA_REAP_COUNTS = payload_counts(
    changed=("reaped",),
    refused=("skipped",),
    quiet="no agenda item was stale enough to abandon",
)


def _agenda_reap_report(result: JobResult) -> StageReport:
    """An unreachable store is not an empty backlog.

    The job returns `ok=True` with `reaped=0, skipped=0` when `AgendaStore()`
    could not be constructed, which the default counting reads as
    `skipped_no_work` — "it ran, there was nothing to do, healthy". It is the
    one shape AR-1 exists to stop: the sweep did not happen and nothing said
    so, and the backlog it guards is the one that starved admission before.
    """
    if result.ok and result.detail == "agenda_store_unavailable":
        return counted(
            RunOutcome.FAILED,
            "the agenda store could not be opened, so nothing was swept",
        )
    return _AGENDA_REAP_COUNTS(result)


def _retention_report(result: JobResult) -> StageReport:
    """Moving and deleting are counted apart, because they are not the same act.

    A night that archived 40 sessions and deleted nothing is not the same
    result as one that deleted 40 files, and a single `changed` number would
    have said it was. A tree that could not be read is `degraded` rather than
    `failed`: the other four still aged, and reporting the whole pass as failed
    would fire the row's `on_failure: alert` for one unreadable directory.
    """
    payload = result.payload if isinstance(result.payload, dict) else {}
    if "trees" not in payload:
        return counted(
            RunOutcome.FAILED, result.detail or "the retention table could not be read"
        )
    moved = int(payload.get("moved") or 0)
    removed = int(payload.get("removed") or 0)
    failed = int(payload.get("failed") or 0)
    errors = payload.get("errors") or ()
    if errors:
        return counted(
            RunOutcome.DEGRADED,
            f"{len(errors)} tree(s) could not be swept: {'; '.join(errors)}",
            changed=moved + removed,
            refused=failed,
        )
    if not moved and not removed:
        return counted(
            RunOutcome.SKIPPED_NO_WORK,
            "nothing had aged past its window",
            refused=failed,
        )
    return counted(
        RunOutcome.SUCCEEDED, result.detail, changed=moved + removed, refused=failed
    )


def _brief_render_report(result: JobResult) -> StageReport:
    """One brief per night, or none. There is no partial brief.

    `skipped_existing` is `skipped_no_work` rather than a success: it means an
    operator already ran `/brief` for that date and this stage deliberately did
    not overwrite what they read — a real outcome, and not the same as having
    written one.
    """
    payload = result.payload if isinstance(result.payload, dict) else {}
    if not result.ok:
        return counted(
            RunOutcome.FAILED, result.detail or "the brief could not be rendered"
        )
    if payload.get("skipped_existing"):
        return counted(RunOutcome.SKIPPED_NO_WORK, result.detail)
    if payload.get("cost_cap_hit"):
        # It wrote a brief, with sections the ceiling stopped it filling.
        return counted(
            RunOutcome.DEGRADED,
            f"{result.detail} — the spend ceiling was reached",
            changed=1,
        )
    return counted(RunOutcome.SUCCEEDED, result.detail, changed=1)


def _conscience_report(result: JobResult) -> StageReport:
    """One drift report per run, or none — there is no partial scrape."""
    if not result.ok:
        return counted(
            RunOutcome.FAILED, result.detail or "the drift report could not be written"
        )
    return counted(RunOutcome.SUCCEEDED, result.detail, changed=1)


def _index_rebuild_report(result: JobResult) -> StageReport:
    """FTS always; FAISS only when embeddings are reachable.

    The job's own docstring calls the embedding-less path "the expected
    degraded mode" and then returns `ok=True` for it. It is exactly what
    `degraded` is for: output below the declared contract, and a searcher that
    can no longer answer a semantic query should not read as a clean night.
    """
    payload = result.payload if isinstance(result.payload, dict) else {}
    if not result.ok or "fts_count" not in payload:
        return counted(
            RunOutcome.FAILED, result.detail or "the index rebuild did not run"
        )
    changed = (
        int(payload.get("replayed") or 0)
        + int(payload.get("removed") or 0)
        + int(payload.get("vault_backfilled") or 0)
    )
    if not payload.get("embedding_available"):
        return counted(
            RunOutcome.DEGRADED,
            f"embeddings unreachable — BM25 rebuilt, vector index not ({result.detail})",
            changed=changed,
        )
    return counted(RunOutcome.SUCCEEDED, result.detail, changed=changed)


def _vault_lint_report(result: JobResult) -> StageReport:
    report = result.payload
    if "orphans" not in report:
        return counted(
            RunOutcome.FAILED, result.detail or "the vault linter did not run"
        )
    failures = len(report.get("failures") or ())
    if failures:
        # `failed`, not `degraded`: a lint pass that crashed is the case the
        # row's `on_failure: alert` exists for, and the engine only alerts when
        # the result is not ok — which `degraded` is. The job's own findings
        # (orphans, stale, contradictions) are a different thing entirely and
        # stay a success below.
        return counted(
            RunOutcome.FAILED,
            f"{failures} lint pass(es) failed: {result.detail}",
        )
    findings = sum(
        len(report.get(key) or ())
        for key in ("orphans", "stale", "contradictions", "missing_hubs")
    )
    # The vault outgrowing its declared bound is a finding with no list of its
    # own; counting only the four lists let an alarm read as a clean night.
    alarm = bool(report.get("scale_alarm"))
    if alarm:
        return counted(
            RunOutcome.DEGRADED,
            f"vault scale alarm; {findings} other finding(s): {result.detail}",
            refused=findings,
        )
    return StageReport(
        outcome=RunOutcome.SUCCEEDED,
        reason=f"{findings} finding(s): {result.detail}" if findings else "",
    )


def _atlas_verify_report(result: JobResult) -> StageReport:
    """The job already decided; this only carries the numbers across.

    Drift is `degraded` rather than `failed` there, and the reading has to
    agree — a check that found something is a check that worked.
    """
    payload = result.payload if isinstance(result.payload, dict) else {}
    if not result.ok:
        return counted(
            RunOutcome.FAILED, result.detail or "the atlas check did not run"
        )
    return counted(
        result.outcome or RunOutcome.SUCCEEDED,
        result.outcome_reason,
        refused=int(payload.get("drift") or 0),
    )


CONSOLIDATE_ROW = register_row(
    Row(
        name=ROW_NAME,
        imports=("leaf_seals",),
        stages=(
            # --- independent deterministic maintenance -------------------
            job_stage(
                name="daily_writer",
                job=DailyWriterJob,
                writes=("scheduler_rollup",),
                budget_seconds=120,
                # Derives one date from fired_at, so a gap has to be walked.
                per_day=True,
                report=payload_counts(
                    changed=("runs",), quiet="no scheduler runs to roll up"
                ),
            ),
            job_stage(
                name="conscience_heartbeat",
                job=ConscienceHeartbeatJob,
                writes=("drift_report",),
                budget_seconds=120,
                report=_conscience_report,
            ),
            job_stage(
                name="work_index_sweep",
                job=WorkIndexSweepJob,
                writes=("pruned_work_index",),
                budget_seconds=180,
                report=payload_counts(
                    changed=("chat_metadata_pruned", "work_index_paths_pruned"),
                    refused=("errors",),
                    quiet="no ghost rows to prune",
                ),
            ),
            job_stage(
                name="interests_decay",
                job=InterestsDecayJob,
                writes=("interest_profile",),
                budget_seconds=60,
                report=payload_counts(
                    changed=("kept_topics",),
                    refused=("pruned_topics",),
                    quiet="no interest profile to decay yet",
                ),
            ),
            job_stage(
                name="agenda_reap",
                job=AgendaReaperJob,
                writes=("agenda_abandonments",),
                budget_seconds=60,
                report=_agenda_reap_report,
            ),
            # --- the leaf tree aggregates, off what capture sealed --------
            job_stage(
                name="leaf_topic_route",
                job=TopicRouteJob,
                reads=("leaf_seals",),
                writes=("topic_trees",),
                budget_seconds=300,
                report=payload_counts(
                    changed=("activated", "sections_written"),
                    # A section already on the tree: seen, and deliberately not
                    # written again. That is the idempotency working, and it is
                    # worth a number rather than a silence.
                    refused=("sections_skipped",),
                    quiet="no seal had a topic to route to",
                ),
            ),
            job_stage(
                name="leaf_digest_daily",
                job=DigestDailyJob,
                reads=("leaf_seals",),
                writes=("global_digest",),
                budget_seconds=300,
                report=payload_counts(
                    changed=("seals",), quiet="no seals were written today"
                ),
            ),
            # --- the vault ------------------------------------------------
            job_stage(
                name="vault_raw_watch",
                job=VaultRawWatchJob,
                writes=("vault_documents",),
                budget_seconds=900,
                report=payload_counts(
                    changed=("auto_ingested",),
                    # Held for the operator, or refused by a safety filter.
                    refused=("ask_queued_count", "auto_failed", "skipped_nonconforming"),
                    quiet="no new files in vault/raw",
                ),
            ),
            job_stage(
                name="vault_lint",
                job=VaultLintJob,
                writes=("vault_findings",),
                after=("vault_raw_watch",),
                budget_seconds=600,
                report=_vault_lint_report,
            ),
            # --- the probe: the one model stage that is not distillation ---
            # No `after` and no edges. It reads roles.yaml and calls each
            # active role's primary directly, so it consumes nothing this row
            # produces and nothing here consumes it — declaring an edge to put
            # it "first" would invent a dependency and a failure cascade with
            # it. Its 05:30 row is gone; a probe the night before a working
            # day is the same backstop it always was.
            job_stage(
                name="provider_probe",
                job=ProviderProbeJob,
                writes=("provider_health",),
                kind=StageKind.MODEL,
                budget_seconds=_MODEL_BUDGET,
                report=_provider_probe_report,
            ),
            # --- the distillation, the only part that calls a model -------
            job_stage(
                name="chat_digest",
                job=ChatDigestJob,
                writes=("chat_digests",),
                kind=StageKind.MODEL,
                budget_seconds=_MODEL_BUDGET,
                # One date per call, and it calls a model — so a gap is walked
                # day by day and bounded, rather than skipped or unbounded.
                per_day=True,
                max_catchup_days=_MODEL_CATCHUP_DAYS,
                report=payload_counts(
                    changed=("wrote",),
                    quiet="yesterday was already digested, or had no transcript",
                ),
            ),
            job_stage(
                name="librarian_heartbeat",
                job=LibrarianHeartbeatJob,
                writes=("memory_records",),
                after=("chat_digest",),
                kind=StageKind.MODEL,
                budget_seconds=_MODEL_BUDGET,
                report=payload_counts(
                    changed=("promoted", "deduped"),
                    refused=("skipped",),
                    quiet="the daily layer had nothing to consolidate",
                ),
            ),
            job_stage(
                name="feedback_sweep",
                job=FeedbackSweepJob,
                writes=("feedback_proposals",),
                kind=StageKind.MODEL,
                budget_seconds=_MODEL_BUDGET,
                per_day=True,
                max_catchup_days=_MODEL_CATCHUP_DAYS,
                report=payload_counts(
                    changed=("proposals",),
                    quiet="no directive-shaped statement went unreflected",
                ),
            ),
            job_stage(
                name="feedback_consolidator",
                job=FeedbackConsolidatorJob,
                writes=("feedback_consolidation",),
                cadence=StageCadence.WEEKLY,
                kind=StageKind.MODEL,
                budget_seconds=_MODEL_BUDGET,
                report=payload_counts(
                    changed=("proposals",),
                    quiet="the active feedback set had nothing to consolidate",
                ),
            ),
            # --- settling the store, then checking it, then repairing it --
            job_stage(
                name="dream_cycle",
                job=DreamCycleJob,
                writes=("memory_promotions",),
                after=("librarian_heartbeat",),
                budget_seconds=600,
                report=payload_counts(
                    changed=("promoted_count",),
                    quiet="nothing recalled often enough to promote",
                ),
            ),
            job_stage(
                name="memory_lint",
                job=MemoryLintJob,
                writes=("memory_findings",),
                after=("dream_cycle", "librarian_heartbeat"),
                budget_seconds=300,
                report=_memory_lint_report,
            ),
            job_stage(
                name="memory_scrub",
                job=MemoryScrubJob,
                reads=("memory_findings",),
                writes=("memory_repairs",),
                budget_seconds=300,
                report=_memory_scrub_report,
            ),
            # --- last, so the derived layers describe the settled store ----
            job_stage(
                name="index_rebuild",
                job=IndexRebuildJob,
                writes=("search_indexes",),
                after=("memory_scrub",),
                budget_seconds=900,
                # The one migrated stage whose retry survives: its row carried
                # max_retries=1/backoff=120 and it calls no model, so a retry
                # costs time and nothing else.
                retries=1,
                retry_backoff_seconds=120,
                report=_index_rebuild_report,
            ),
            job_stage(
                name="atlas_build",
                job=AtlasBuildJob,
                writes=("atlas",),
                # `after`, not `reads`: the atlas is derived from the memory
                # store and the wiki, not from anything scrub returns. But it
                # must not map links that are about to be repaired, and the
                # ordering that guarantees it is the declaration rather than
                # the position of this block.
                after=("memory_scrub", "index_rebuild"),
                budget_seconds=600,
                report=payload_counts(
                    changed=("nodes",),
                    quiet="the memory store and the vault are both empty",
                ),
            ),
            job_stage(
                name="atlas_verify",
                job=AtlasVerifyJob,
                # A real data edge, and the only one besides memory_lint →
                # memory_scrub: verifying the file the build did not write is
                # verifying last night's, and every ordinary edit since would
                # read as drift.
                reads=("atlas",),
                cadence=StageCadence.WEEKLY,
                budget_seconds=600,
                report=_atlas_verify_report,
            ),
            # --- last: age what the night has finished reading -------------
            job_stage(
                name="retention",
                job=RetentionJob,
                writes=("aged_trees",),
                # `after`, not `reads`: nothing here consumes what retention
                # produces, but retention ages the session drawer that
                # `chat_digest` reads. At the shipped windows they cannot
                # collide — the digest reads yesterday and the drawer keeps a
                # week — and this is what keeps that true if an operator
                # shortens the window. A plain ordering edge, so a failed
                # digest does not stop the machine tidying up after itself.
                after=("chat_digest",),
                budget_seconds=300,
                report=_retention_report,
            ),
            job_stage(
                name="memory_relink",
                job=MemoryRelinkJob,
                # The atlas finds the orphans; this repairs them. The trigger
                # is that data edge, not a clock — and it runs after the check
                # rather than before it, because writing to the store between
                # the build and its verification would report every repair as
                # drift. `after` on a WEEKLY stage does not cascade, so the
                # six nights the check is not due cost this nothing.
                reads=("atlas",),
                after=("atlas_verify",),
                kind=StageKind.MODEL,
                budget_seconds=600,
                report=payload_counts(
                    changed=("linked",),
                    refused=("over_cap",),
                    quiet="nothing the atlas found needed re-linking",
                ),
            ),
            # --- the last word of the night ------------------------------
            # Last because that is the whole point: an 08:00 row read files
            # written before the pipeline touched them.
            #
            # `after` and never `reads`, on all three. The brief reads the
            # canonical store — memory, the agenda, the vault, the runtime
            # logs — not any artifact this row publishes, so a data edge would
            # be an invention. It would also be the expensive kind: `reads`
            # cascades, so one unreachable provider failing `chat_digest`
            # would skip the brief entirely, and a day with a degraded digest
            # still deserves the brief that says so.
            #
            # `provider_probe` is in the list because its findings are in the
            # brief's runtime block. It ran at 05:30 and the brief at 08:00,
            # so the probe has always preceded it; dropping the edge would
            # have quietly aged that section by a day.
            job_stage(
                name="brief_render",
                job=BriefRenderJob,
                writes=("daily_brief",),
                after=("retention", "memory_relink", "provider_probe"),
                kind=StageKind.MODEL,
                budget_seconds=_MODEL_BUDGET,
                report=_brief_render_report,
            ),
        ),
    )
)


__all__ = ["CONSOLIDATE_ROW", "ROW_NAME"]
