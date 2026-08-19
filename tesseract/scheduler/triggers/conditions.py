"""What a row waits for, when it does not wait for a clock.

A cron cadence answers "how often", and for some work that is the wrong
question: a weekly refinement pass fires on two data points as readily as on
two hundred, and a nightly reap of orphan processes leaves them running until
04:30 when the reason to reap them is that the app just started. Both are
events wearing an hour.

So a row may declare `when: <condition>` instead of `cadence: <cron>`. The row
stays the unit — registration, `enabled`, its config block, `on_failure`, the
retry policy, the run record and the operator's override all work unchanged —
and only the question the engine asks each tick changes.

**Position, not history.** A volume condition counts what has arrived since the
last time its row fired, and that position is a timestamp in the pipeline's
`WatermarkStore`. Reusing that store rather than adding one is deliberate: it
already answers "how far has this reader consumed its input", already writes
atomically, and a trigger is a reader with a position. `boot` is the exception
and says so — its position is the process, so it is held in memory and a
restart is precisely the event.

**A condition is cheap or it does not belong here.** Every armed condition is
evaluated on the engine's existing 60-second tick, so reading a whole file per
tick would be a poll wearing an event's clothes. Each reads one small file or
lists one directory, and does it off the loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class ConditionError(ValueError):
    """A `when:` a row declares that this file cannot honour."""


@dataclass(frozen=True)
class TriggerContext:
    """Everything a condition may look at."""

    job_name: str
    # The row's `when_config` block — the operator's thresholds for the
    # FIRING, kept apart from the job's own `config` so that "how often does
    # this happen" is not buried inside "what does it do".
    config: dict[str, Any]
    now: datetime
    # When this row last fired, from the watermark store. `None` means never —
    # every condition treats that as "everything on disk is new", which is what
    # makes a fresh install fire once as soon as it has enough to work with.
    watermark: datetime | None
    # Whether this process has already fired this row. The `boot` condition's
    # whole position; ignored by the volume conditions, whose position is
    # durable.
    fired_this_process: bool


@dataclass(frozen=True)
class Verdict:
    """Fire or do not, and the sentence the operator reads either way."""

    fire: bool
    reason: str


@dataclass(frozen=True)
class Condition:
    """One named answer to "has the thing that should fire this happened".

    `required_config` is checked when the engine arms, not when the condition
    is first evaluated. A threshold missing from yaml is a row that would sit
    silent forever, and finding that out at boot is the difference between a
    loud start-up error and a job nobody notices never ran.
    """

    name: str
    summary: str
    required_config: tuple[str, ...]
    evaluate: Callable[[TriggerContext], Awaitable[Verdict]]


def _home() -> Path:
    """`TESSERACT_HOME` resolved AT CALL TIME — never an import constant, so a
    test that sets it before evaluating never counts production rows."""
    from tesseract.paths import TESSERACT_HOME

    override = os.environ.get("TESSERACT_HOME")
    return Path(override).resolve() if override else TESSERACT_HOME


def _threshold(ctx: TriggerContext, key: str) -> int:
    raw = ctx.config.get(key)
    if not isinstance(raw, int) or raw < 1:
        raise ConditionError(
            f"row {ctx.job_name!r}: `when_config.{key}` must be a positive integer, "
            f"got {raw!r} — it is how much has to arrive before this runs"
        )
    return raw


# ── boot ──────────────────────────────────────────────────


async def _at_boot(ctx: TriggerContext) -> Verdict:
    if ctx.fired_this_process:
        return Verdict(False, "already ran since this start")
    return Verdict(True, "the app started")


# ── volume: new skill-usage rows ──────────────────────────


async def _skill_usage_volume(ctx: TriggerContext) -> Verdict:
    # Imported here, not at module scope: `brain/` reaches back into the
    # scheduler in places, and a condition file the engine imports at boot is
    # the wrong place to discover that.
    from tesseract.brain.skill_usage import read_usage

    minimum = _threshold(ctx, "min_new_rows")
    rows = await asyncio.to_thread(read_usage)
    fresh = _count_newer(rows, ctx.watermark)
    if fresh < minimum:
        return Verdict(
            False, f"{fresh} of {minimum} new skill uses since the last pass"
        )
    return Verdict(True, f"{fresh} new skill uses since the last pass")


def _is_newer(stamp: Any, watermark: datetime | None) -> bool:
    """Whether an ISO timestamp field is past the row's position.

    Shared by every volume condition: a stamp that is missing or unparseable
    counts as NOT new, because a condition may not fire on evidence it could
    not read. `None` watermark means the row has never fired, so everything on
    disk is new — which is what makes a fresh install fire once it has enough.
    """
    if not isinstance(stamp, str):
        return False
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return watermark is None or when > watermark


def _count_newer(rows: list[dict[str, Any]], watermark: datetime | None) -> int:
    return sum(1 for row in rows if _is_newer(row.get("ts"), watermark))


# ── volume: new daily digests ─────────────────────────────


async def _digest_volume(ctx: TriggerContext) -> Verdict:
    minimum = _threshold(ctx, "min_new_digests")
    fresh = await asyncio.to_thread(_count_new_digests, ctx.config, ctx.watermark)
    if fresh < minimum:
        return Verdict(False, f"{fresh} of {minimum} new daily digests")
    return Verdict(True, f"{fresh} new daily digests since the last pass")


def _count_new_digests(config: dict[str, Any], watermark: datetime | None) -> int:
    """Digests are `memory-store/daily/YYYY-MM-DD.md`, so the day is the
    filename. Counted from the name rather than the mtime: a store copied to a
    new machine keeps its dates and loses its timestamps, and locked decision 5
    is that copying canonical state carries the assistant with it."""
    override = config.get("daily_dir")
    daily = Path(override) if override else _home() / "memory-store" / "daily"
    if not daily.exists():
        return 0
    cutoff = watermark.date() if watermark else None
    count = 0
    try:
        files = list(daily.glob("*.md"))
    except OSError:
        return 0
    for path in files:
        try:
            day = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if cutoff is None or day > cutoff:
            count += 1
    return count


# ── volume: real traffic said a provider is down ──────────


async def _provider_failover(ctx: TriggerContext) -> Verdict:
    """New `production_tripwire` rows in `provider-health/`.

    A tripwire is written when a chain FAILED OVER — the primary errored under
    real traffic and something behind it answered. That is the moment a probe
    is worth paying for, and the nightly stage is the backstop for everything
    it misses: waiting for 23:00 to find out a key died at breakfast is a day
    of every role quietly running on its fallback.

    Counted, not fired per event, for two reasons. One chain failing over
    writes MORE THAN ONE row in a few seconds — the pair on this machine is
    seven seconds apart, from a single incident — and a probe calls every
    active role's primary, so firing per row would bill a sweep per retry. The
    shipped threshold is that measured pair, so one incident fires it and one
    retry does not; a number above it would mean the case this exists for
    never firing at all. And the
    watchman already reports a tripwire within the hour by reading this same
    file; what a probe adds is CURRENT state rather than history, which is
    worth a threshold rather than a hair trigger.
    """
    minimum = _threshold(ctx, "min_new_tripwires")
    fresh = await asyncio.to_thread(_count_new_tripwires, ctx.watermark)
    if fresh < minimum:
        return Verdict(
            False, f"{fresh} of {minimum} new provider failovers since the last probe"
        )
    return Verdict(True, f"{fresh} new provider failovers since the last probe")


def _count_new_tripwires(watermark: datetime | None) -> int:
    """One small JSONL per role, and only the tail matters.

    Rows carry `probed_at` and `source`; anything that is not a
    `production_tripwire` is a probe's own result and says nothing about real
    traffic. An unreadable file counts zero rather than raising — a condition
    that cannot be read must not take the tick down with it.
    """
    from tesseract.orchestrator.provider_health import provider_health_dir

    root = provider_health_dir()
    if not root.is_dir():
        return 0
    count = 0
    for path in sorted(root.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("source") != "production_tripwire":
                continue
            if _is_newer(row.get("probed_at"), watermark):
                count += 1
    return count


CONDITIONS: dict[str, Condition] = {
    c.name: c
    for c in (
        Condition(
            name="boot",
            summary="the app started",
            required_config=(),
            evaluate=_at_boot,
        ),
        Condition(
            name="skill_usage_volume",
            summary="enough new skill uses have been logged to judge one",
            required_config=("min_new_rows",),
            evaluate=_skill_usage_volume,
        ),
        Condition(
            name="digest_volume",
            summary="enough new daily digests have accumulated to read across",
            required_config=("min_new_digests",),
            evaluate=_digest_volume,
        ),
        Condition(
            name="provider_failover",
            summary="real traffic has failed over from a provider's primary",
            required_config=("min_new_tripwires",),
            evaluate=_provider_failover,
        ),
    )
}


def condition(name: str) -> Condition:
    """The named condition, or a `ConditionError` naming the ones that exist."""
    found = CONDITIONS.get(name)
    if found is None:
        known = ", ".join(sorted(CONDITIONS)) or "(none)"
        raise ConditionError(
            f"unknown trigger {name!r} — schedule.yaml `when:` must name one of: {known}"
        )
    return found


def check_row(job_name: str, when: str, when_config: dict[str, Any]) -> None:
    """Raise unless this row's `when:` can actually fire. Called at boot."""
    cond = condition(when)
    missing = [key for key in cond.required_config if key not in (when_config or {})]
    if missing:
        raise ConditionError(
            f"row {job_name!r} fires on {when!r} and its `when_config` is missing "
            f"{missing} — without it the row would never fire and nothing would say so"
        )


__all__ = [
    "CONDITIONS",
    "Condition",
    "ConditionError",
    "TriggerContext",
    "Verdict",
    "check_row",
    "condition",
]
