"""RetentionJob — sweep every tree in the retention table, once.

Runs as the `retention` stage of the `consolidate` row. It replaces the
`sessions_archive` (04:00) and `observer_log_prune` (16:30) rows and takes the
file-ageing half of the janitor's sweep; the janitor keeps process reaping,
which is a live problem and runs at every supervisor boot as well.

The sweeps are independent, so they run concurrently and one failing is
recorded rather than aborting the rest — the same contract `janitor/runner.py`
holds them to, for the same reason: a tree that could not be read is not a
reason to stop ageing the other four.
"""

from __future__ import annotations

import asyncio
import logging
import time

from tesseract.retention import record
from tesseract.retention.policy import RetentionError, Swept, load_live
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


class RetentionJob(BaseJob):
    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            policies = load_live()
        except RetentionError as exc:
            # The table not holding is a config error, and reporting it beats
            # ageing four trees on a policy the fifth contradicts.
            return _result(ctx, t0, ok=False, detail=str(exc), payload={})
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("retention: the table could not be read")
            return _result(ctx, t0, ok=False, detail=f"unhandled: {exc!r}", payload={})

        results = await asyncio.gather(
            *(asyncio.to_thread(policy.run) for policy in policies),
            return_exceptions=True,
        )

        per_tree: dict[str, dict[str, int]] = {}
        errors: list[str] = []
        total = Swept()
        for policy, outcome in zip(policies, results):
            key = policy.tree.key
            if isinstance(outcome, BaseException):
                errors.append(f"{key}: {_why(outcome)}")
                log.exception("retention: %s failed", key, exc_info=outcome)
                continue
            per_tree[key] = {
                "moved": outcome.moved,
                "removed": outcome.removed,
                "failed": outcome.failed,
                "held": outcome.held,
                "keep_days": policy.keep_days,
                "action": policy.action.value,
            }
            total += outcome

        detail = (
            f"moved={total.moved} removed={total.removed} "
            f"failed={total.failed} held={total.held} over {len(per_tree)} tree(s)"
        )
        if errors:
            detail += f"; {len(errors)} tree(s) errored"
        # A `StageReport` carries one outcome and two totals, so the per-tree
        # half of this payload has nowhere else to go. Written before the
        # result is returned, and never able to change it.
        record.write(per_tree, errors)
        return _result(
            ctx,
            t0,
            # A tree that raised is a failure of this pass even though the
            # others ran: silence here is what let four scattered policies go
            # unexamined for as long as they did.
            ok=not errors,
            detail=detail,
            payload={
                "trees": per_tree,
                "moved": total.moved,
                "removed": total.removed,
                "failed": total.failed,
                "held": total.held,
                "errors": errors,
            },
        )


def _why(exc: BaseException) -> str:
    """What went wrong, without the path it went wrong ON.

    This list is not a log line any more: it is persisted for the Autonomy
    panel and relayed to whatever channel asks. An `OSError`'s `repr` carries
    the filename, which sits under the operator's home directory and therefore
    carries their username, so what reaches a screen is the class and the
    system's own reason. The whole exception, path and traceback included, goes
    to the backend log on the line above, which is where somebody debugging
    this wants it.
    """
    reason = getattr(exc, "strerror", None)
    return f"{type(exc).__name__}: {reason}" if reason else type(exc).__name__


def _result(
    ctx: JobContext, t0: float, *, ok: bool, detail: str, payload: dict
) -> JobResult:
    return JobResult(
        job_name=ctx.job_name,
        run_id=ctx.run_id,
        ok=ok,
        detail=detail,
        payload=payload,
        duration_ms=(time.monotonic() - t0) * 1000.0,
    )


__all__ = ["RetentionJob"]
