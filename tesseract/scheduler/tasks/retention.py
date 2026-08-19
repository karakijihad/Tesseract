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

from tesseract.retention.policy import Policy, RetentionError, Swept, load_live
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
            *(asyncio.to_thread(_sweep_one, policy) for policy in policies),
            return_exceptions=True,
        )

        per_tree: dict[str, dict[str, int]] = {}
        errors: list[str] = []
        total = Swept()
        for policy, outcome in zip(policies, results):
            key = policy.tree.key
            if isinstance(outcome, BaseException):
                errors.append(f"{key}: {outcome!r}")
                log.exception("retention: %s failed", key, exc_info=outcome)
                continue
            per_tree[key] = {
                "moved": outcome.moved,
                "removed": outcome.removed,
                "failed": outcome.failed,
                "keep_days": policy.keep_days,
                "action": policy.action.value,
            }
            total += outcome

        detail = (
            f"moved={total.moved} removed={total.removed} "
            f"failed={total.failed} over {len(per_tree)} tree(s)"
        )
        if errors:
            detail += f"; {len(errors)} tree(s) errored"
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
                "errors": errors,
            },
        )


def _sweep_one(policy: Policy) -> Swept:
    return policy.run()


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
