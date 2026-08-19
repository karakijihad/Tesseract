"""Asking a row's condition whether it should fire, and remembering that it did.

Split from `conditions.py` so the conditions stay a readable list of "what
counts as the event" with no bookkeeping in among them. This half owns the
position: which key a row's watermark lives under, when it advances, and the
rule that it advances only on an actual fire.

**The watermark advances at dispatch, not at completion.** A volume condition
that waited for the run to finish would re-fire on the next tick while the
first run was still going — the same double-fire the cron path guards with its
60-second dedupe, except a model call would be billed twice. A run that fails
is covered by the row's own retry policy and by the next arrival of whatever it
counts, not by re-reading a window it has already been given.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from tesseract.scheduler.pipeline.artifacts import WatermarkStore
from tesseract.scheduler.triggers.conditions import (
    ConditionError,
    TriggerContext,
    Verdict,
    condition,
)

log = logging.getLogger(__name__)

# Namespaced so a trigger row and a pipeline stage of the same name cannot
# read each other's position out of the one file they share.
WATERMARK_PREFIX = "trigger:"


def watermark_key(job_name: str) -> str:
    return f"{WATERMARK_PREFIX}{job_name}"


async def evaluate(
    job_name: str,
    when: str,
    when_config: dict[str, Any],
    *,
    now: datetime,
    fired_this_process: bool,
    watermarks: WatermarkStore,
) -> Verdict:
    """Whether `job_name` should fire now. Never raises past this call.

    A condition that throws is reported as "not firing, and here is why" rather
    than taken down with the tick: one unreadable usage log must not stop every
    other row in the registry from being asked.
    """
    try:
        cond = condition(when)
        ctx = TriggerContext(
            job_name=job_name,
            config=when_config or {},
            now=now,
            watermark=watermarks.get(watermark_key(job_name)),
            fired_this_process=fired_this_process,
        )
        return await cond.evaluate(ctx)
    except ConditionError as exc:
        return Verdict(False, str(exc))
    except Exception as exc:  # noqa: BLE001 — one bad condition is not a bad tick
        log.exception("trigger %s (%s): condition raised", job_name, when)
        return Verdict(False, f"the condition could not be read: {exc!r}")


def record_fired(job_name: str, fired_at: datetime, watermarks: WatermarkStore) -> None:
    """Advance the row's position. Best-effort: a lost write costs one extra
    run, and refusing to dispatch because the position could not be saved would
    cost the run itself."""
    try:
        watermarks.set(watermark_key(job_name), fired_at)
    except Exception:  # noqa: BLE001
        log.exception("trigger %s: watermark write failed — it may run again", job_name)


__all__ = ["WATERMARK_PREFIX", "evaluate", "record_fired", "watermark_key"]
