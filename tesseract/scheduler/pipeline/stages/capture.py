"""The capture row — what must not wait for the night.

Three stages, deterministic, no model ever: leaves are extracted and buffered,
a buffer that has reached its threshold is sealed, and a conversation that has
gone quiet earns its recap. They fire in minutes because the capture window is
lost otherwise, and their thresholds — not the clock — decide when anything
happens.

`leaf_seal` used to be its own `*/15` row while intake ran `*/5`. On one row it
is checked every five minutes instead of every fifteen. Nothing about when it
SEALS changes: `max_buffer_leaves` and `max_buffer_age_seconds` still decide
that, and checking three times as often only means a ripe buffer waits less.

`conversation_reflect` is the funnel: the Mirror's chats and every channel's
read into one shape, one recap each, tagged with the door they came through. It
replaces a `*/5` row that swept one channel through the bridge's own memory,
and the deterministic half of an idle observer fire that cost a remote call to
produce a log line.
"""

from __future__ import annotations

from tesseract.orchestrator.outcome import RunOutcome
from tesseract.scheduler.pipeline.job_stage import counted, job_stage
from tesseract.scheduler.pipeline.registry import Row, register_row
from tesseract.scheduler.pipeline.stage import StageCadence, StageKind, StageReport
from tesseract.scheduler.tasks.conversation_reflect import ConversationReflectJob
from tesseract.scheduler.tasks.leaf_intake import LeafIntakeJob
from tesseract.scheduler.tasks.leaf_seal import SealJob
from tesseract.scheduler.types import JobResult

ROW_NAME = "capture"


def _intake_report(result: JobResult) -> StageReport:
    extract = result.payload.get("extract") or {}
    append = result.payload.get("append") or {}
    admitted = int(extract.get("admitted") or 0)
    appended = int(append.get("appended") or 0)
    dropped = int(extract.get("dropped") or 0)
    errors = int(extract.get("errors") or 0) + int(append.get("errors") or 0)
    changed = admitted + appended

    if not result.ok:
        return counted(
            RunOutcome.FAILED,
            result.detail or "leaf intake failed without saying why",
            changed=changed,
            refused=dropped,
        )
    if errors:
        return counted(
            RunOutcome.DEGRADED,
            f"{errors} leaf/leaves could not be processed this tick",
            changed=changed,
            refused=dropped,
        )
    if not changed and not dropped:
        return counted(RunOutcome.SKIPPED_NO_WORK, "no leaves were waiting")
    return counted(RunOutcome.SUCCEEDED, changed=changed, refused=dropped)


def _seal_report(result: JobResult) -> StageReport:
    payload = result.payload
    walked = int(payload.get("buffers_walked") or 0)
    seals = int(payload.get("seals_written") or 0)
    missing = int(payload.get("leaves_missing") or 0)

    if not result.ok:
        return counted(
            RunOutcome.FAILED,
            result.detail or "sealing failed without saying why",
            changed=seals,
            refused=missing,
        )
    if missing and not seals:
        # Every leaf a buffer pointed at was gone. The buffer is cleared so the
        # next tick does not loop on it, but nothing was sealed and saying
        # "succeeded" would hide a hole in the capture path.
        return counted(
            RunOutcome.DEGRADED,
            f"{missing} buffered leaf/leaves were missing and nothing sealed",
            refused=missing,
        )
    if not seals:
        return counted(
            RunOutcome.SKIPPED_NO_WORK,
            "no buffer had reached its size or age threshold"
            if walked
            else "no buffers were waiting",
        )
    return counted(RunOutcome.SUCCEEDED, changed=seals, refused=missing)


def _reflect_report(result: JobResult) -> StageReport:
    payload = result.payload
    # A conversation that continued had its ONE record amended; a new one had
    # its record created. Both changed the library and neither is a duplicate,
    # so both count as changed.
    written = int(payload.get("written") or 0) + int(payload.get("amended") or 0)
    blocked = int(payload.get("blocked") or 0)
    failed = int(payload.get("failed") or 0)
    unreadable = int(payload.get("unreadable_sources") or 0)

    if not result.ok:
        return counted(
            RunOutcome.FAILED,
            result.detail or "the funnel failed without saying why",
            changed=written,
            refused=blocked,
        )
    if failed or unreadable:
        # A source that could not be read is a hole in capture, not a quiet
        # tick: the conversations behind it are invisible rather than absent.
        return counted(
            RunOutcome.DEGRADED,
            (
                f"{unreadable} source(s) unreadable and {failed} recap(s) could "
                "not be written"
            ),
            changed=written,
            refused=blocked,
        )
    if not written and not blocked:
        return counted(
            RunOutcome.SKIPPED_NO_WORK,
            "no conversation has said anything since its last recap",
        )
    return counted(RunOutcome.SUCCEEDED, changed=written, refused=blocked)


CAPTURE_ROW = register_row(
    Row(
        name=ROW_NAME,
        stages=(
            job_stage(
                name="leaf_intake",
                job=LeafIntakeJob,
                writes=("leaf_buffers",),  # recorded; sealing reads the files
                cadence=StageCadence.CONTINUOUS,
                kind=StageKind.DETERMINISTIC,
                budget_seconds=120,
                report=_intake_report,
            ),
            job_stage(
                name="leaf_seal",
                job=SealJob,
                # `after`, not `reads`. Sealing walks the buffer files on disk;
                # it does not consume anything intake returned, and as two cron
                # rows a crashing intake left sealing running. A cascading edge
                # would let a systemic intake failure halt sealing too, so
                # buffers filled by earlier ticks would never close — the same
                # argument leaf_intake's own docstring makes for running append
                # unconditionally.
                after=("leaf_intake",),
                writes=("leaf_seals",),
                cadence=StageCadence.CONTINUOUS,
                kind=StageKind.DETERMINISTIC,
                budget_seconds=120,
                report=_seal_report,
            ),
            job_stage(
                name="conversation_reflect",
                job=ConversationReflectJob,
                # `after`, for the same reason sealing is: the funnel reads the
                # session and channel stores, not anything the two leaf stages
                # produced, and a leaf extraction that fails must not stop a
                # conversation being remembered.
                after=("leaf_seal",),
                writes=("conversation_recaps",),
                cadence=StageCadence.CONTINUOUS,
                kind=StageKind.DETERMINISTIC,
                budget_seconds=120,
                report=_reflect_report,
                # It selects against a window ending at `fired_at`, so one
                # call can only ever cover the last two days — a machine off
                # over a weekend came back, asked Monday's question three
                # times, and lost Friday's conversations with nothing to say
                # so. The walk is what a gap needs, and the overlap it creates
                # costs nothing: a conversation already recapped is
                # `up_to_date` on the second day that sees it.
                per_day=True,
            ),
        ),
    )
)


__all__ = ["CAPTURE_ROW", "ROW_NAME"]
