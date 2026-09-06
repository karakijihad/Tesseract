"""What the runtime would change about itself, read off its own gauges.

AR-24 made the spend observable and every reader of it is a panel. This is the
job that reads the same figures and files a card: what it would change, the
seam that would move, and the numbers it read, so the operator can check the
claim against the same file the panel draws.

**It proposes and never writes.** `roles.yaml`, `schedule.yaml` and
`retention.yaml` are untouched by this job. The change happens on the
approval, in `routes/workspace.py::_commit_tuning_proposal`, through the seam
that already owns the field. That split is the whole point: the runtime may
notice a cap is wrong; it may not decide what it should be.

**What may be proposed at all is `scheduler/proposals.py`**, the closed list
this job shares with `working_set_review`. A kind that is not declared there
cannot be filed, and a kind whose gauge does not exist on this machine is
refused rather than guessed at.

Three rules it holds at once, each of which has been a way to get this wrong
somewhere else in this tree:

1. **Silence is not evidence.** A ledger covering three days makes every role
   look like it never approaches its cap. The run refuses to judge until the
   window holds `min_days_to_judge` days, and a role that spent nothing at all
   is not a role whose ceiling is too high.
2. **Nothing to change means no card**, not a card saying nothing changed.
3. **A raise and a drop are different claims.** A role at its cap is measured
   by how many days it hit it; a role nowhere near one is measured by its
   busiest day. Reading both off the same average would let a quiet fortnight
   with one expensive day propose a cut.

Fired on volume rather than on a clock, the way every other proposer here is:
a cadence reads two days of ledger as readily as twenty.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from tesseract.orchestrator.outcome import RunOutcome
from tesseract.scheduler import proposals
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.tasks import _card
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

#: The one card this job files. Decided in the inbox like any other; the
#: approve path is `routes/workspace.py::_commit_tuning_proposal`.
CARD_KIND = "tuning_proposal"


@dataclass
class Spending:
    """What the ledger says each role spent, a day at a time, and the ceiling
    each is held under right now."""

    per_role_day: dict[str, dict[date, float]] = field(default_factory=dict)
    caps: dict[str, float] = field(default_factory=dict)
    days_seen: int = 0
    #: False when the ceilings could not be read at all, which is a different
    #: answer from a machine that declares none.
    caps_read: bool = True


@dataclass(frozen=True)
class Change:
    role: str
    direction: str
    current_usd: float
    proposed_usd: float
    days_at_cap: int
    busiest_day_usd: float
    #: How many days THIS ROLE appears on in the window. Not the window's own
    #: day count, which is the union across every role and reads as evidence
    #: this change does not have.
    days_seen: int

    def as_json(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "direction": self.direction,
            "current_usd": round(self.current_usd, 4),
            "proposed_usd": round(self.proposed_usd, 4),
            "days_at_cap": self.days_at_cap,
            "busiest_day_usd": round(self.busiest_day_usd, 4),
            "days_seen": self.days_seen,
        }


class RuntimeTuningJob(BaseJob):
    uses_llm = False

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            cfg = ctx.config or {}
            # Per KIND, not per job. Item 6's whole point is that a kind which
            # keeps being declined can be silenced without silencing the rest,
            # and gating the run on one of them would take the others with it
            # the moment there is more than one.
            on = [key for key in MY_KINDS if _kind_is_on(cfg, key)]
            if not on:
                return self._closed(
                    ctx, t0, RunOutcome.SKIPPED_NO_WORK,
                    "every proposal kind this job files is switched off",
                    {"filed": 0},
                )

            queue = _card.store(ctx)
            # Keyed on the kind the card CARRIES, not on the event kind, which
            # every kind this job files shares. One open ceiling card blocking
            # a playbook proposal indefinitely would read, from the run log,
            # as nothing to say.
            todo = [key for key in on if key not in _waiting_kinds(queue)]
            if not todo:
                return self._closed(
                    ctx, t0, RunOutcome.SKIPPED_NO_WORK,
                    "a tuning proposal is already waiting to be decided",
                    {"filed": 0},
                )

            window_days = int(cfg.get("window_days", 14))
            min_days = int(cfg.get("min_days_to_judge", 7))
            spending = await _read_spending(ctx, window_days)
            if spending.days_seen < min_days:
                # Rule 1.
                return self._closed(
                    ctx, t0, RunOutcome.SKIPPED_NO_WORK,
                    f"the ledger covers {spending.days_seen} of the last "
                    f"{window_days} days, which is under the {min_days} it takes "
                    "to say whether a ceiling is set right",
                    {"filed": 0, "days_seen": spending.days_seen},
                )

            if not spending.caps_read:
                # Never as a quiet day. A job that could not read the ceilings
                # has judged nothing, and reporting that as "everything is
                # fine" is how a real misconfiguration stays invisible.
                return self._closed(
                    ctx, t0, RunOutcome.DEGRADED,
                    "the daily spending limits could not be read, so nothing "
                    "was judged against them",
                    {"filed": 0, "days_seen": spending.days_seen},
                )

            changes = _propose_ceilings(spending, cfg) if "ceiling" in todo else []
            if not changes:
                # Rule 2.
                return self._closed(
                    ctx, t0, RunOutcome.SKIPPED_NO_WORK,
                    "every ceiling matches what the roles under it spend",
                    {"filed": 0, "days_seen": spending.days_seen},
                )

            filed = await _file_card(ctx, queue, changes, spending, window_days)
            if not filed:
                return self._closed(
                    ctx, t0, RunOutcome.DEGRADED,
                    "the proposal could not be filed as a card, so nobody was asked",
                    {"filed": 0},
                )
            return self._closed(
                ctx, t0, RunOutcome.SUCCEEDED, "",
                {
                    "filed": 1,
                    "days_seen": spending.days_seen,
                    "changes": [c.as_json() for c in changes],
                },
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("runtime_tuning crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )

    def _closed(
        self,
        ctx: JobContext,
        t0: float,
        outcome: RunOutcome,
        reason: str,
        payload: dict[str, Any],
    ) -> JobResult:
        return JobResult(
            job_name=ctx.job_name,
            run_id=ctx.run_id,
            ok=True,
            detail=reason or f"filed {payload.get('filed', 0)} proposal",
            outcome=outcome,
            outcome_reason=reason,
            payload=payload,
            duration_ms=(time.monotonic() - t0) * 1000.0,
        )


#: Every kind this job files. `working_set_review` files the other two, and a
#: kind on neither list is declared and unbuilt.
MY_KINDS: tuple[str, ...] = ("ceiling",)


def _waiting_kinds(queue: Any) -> set[str]:
    """The proposal kinds already waiting on the operator.

    `_card.one_waiting` answers the same question for a whole event kind,
    which is the right grain for a job that files one shape. This job will
    file several through one event kind, so the payload's own `kind` is what
    tells them apart.
    """
    try:
        rows = queue.list_events(kinds=(CARD_KIND,), status="pending")
    except Exception:  # noqa: BLE001 — an unreadable queue reads as empty
        log.warning("runtime_tuning: the queue could not be read", exc_info=True)
        return set()
    return {str((row.payload or {}).get("kind") or "") for row in rows}


def _kind_is_on(cfg: dict[str, Any], key: str) -> bool:
    """Whether the operator has left this proposal kind switched on.

    Absent means on. A kind is declared in code and shipped enabled; the
    switch exists so a kind that keeps being declined can be silenced without
    silencing the rest, which is a different thing from never having had it.
    """
    switches = cfg.get("kinds")
    if not isinstance(switches, dict):
        return True
    return bool(switches.get(key, True))


async def _read_spending(ctx: JobContext, window_days: int) -> Spending:
    import asyncio

    return await asyncio.to_thread(_read_spending_blocking, ctx, window_days)


def _read_spending_blocking(ctx: JobContext, window_days: int) -> Spending:
    # **The panel's reader, not a second one.** AR-24 left one parser of this
    # ledger and a guard that fails a second, for the reason a second reader
    # of any record here always turns out to be: two answers to what was
    # spent, differing by how far back each one reads.
    from tesseract.lib.clock import to_local
    from tesseract.mirror.server.routes.autonomy_history import role_days

    out = Spending()
    path = _ledger_path(ctx)
    if path is None or not path.is_file():
        return out
    # **The operator's day, not the clock's.** A ledger row carries
    # `local_date`, which is already their calendar day, so a cutoff taken
    # off the UTC instant would compare two different calendars and the
    # window would start and end a day early for the last hours of every
    # evening east of Greenwich.
    cutoff = to_local(ctx.fired_at).date() - timedelta(days=window_days)
    out.per_role_day = role_days(path, since=cutoff)
    out.days_seen = len({day for byday in out.per_role_day.values() for day in byday})
    caps = _caps(ctx)
    out.caps_read = caps is not None
    out.caps = caps or {}
    return out


def _ledger_path(ctx: JobContext) -> Path | None:
    """Where the ledger is, asked of the ledger.

    Its filename is declared in `providers.yaml::cost_tracking.log_file`, so a
    path composed here would be a second answer to where the spend is written.
    `routes/autonomy_history.py` asks the same object the same question.
    """
    app = ctx.app
    if app is None:
        return None
    override = (ctx.config or {}).get("ledger_path")
    if override:
        return Path(override)
    return getattr(app.get("cost_ledger"), "log_path", None)


def _caps(ctx: JobContext) -> dict[str, float] | None:
    """What each role's daily ceiling is, off the live config.

    The in-memory snapshot rather than a second read of `roles.yaml`: the
    route that writes a ceiling syncs it, so this is the same value every
    other reader of a cap is using, and a job holding its own parse of that
    file would be the second answer this phase exists to avoid.

    **A snapshot that cannot be read is not a machine with no ceilings.** The
    two look identical from here and they mean opposite things: one is a
    quiet fortnight, the other is a job that cannot judge anything and said
    so as "every ceiling matches what the roles under it spend". `None` is
    the difference, and the caller turns it into a degraded run.
    """
    app = ctx.app
    if app is None:
        return None
    try:
        cost_cfg = app["config"].models.get("cost_tracking") or {}
    except Exception:  # noqa: BLE001 — reported, never raised out of a job
        log.warning("runtime_tuning: the cost snapshot could not be read", exc_info=True)
        return None
    out: dict[str, float] = {}
    for role, cap in (cost_cfg.get("per_role") or {}).items():
        try:
            value = float(cap)
        except (TypeError, ValueError):
            continue
        if value > 0:
            out[str(role)] = value
    return out


def _propose_ceilings(spending: Spending, cfg: dict[str, Any]) -> list[Change]:
    at_cap = float(cfg.get("at_cap_fraction", 0.9))
    days_to_raise = int(cfg.get("days_at_cap_to_raise", 3))
    raise_by = float(cfg.get("raise_factor", 2.0))
    idle = float(cfg.get("idle_fraction", 0.1))
    headroom = float(cfg.get("lower_headroom", 2.0))

    min_days = int(cfg.get("min_days_per_role", 3))

    out: list[Change] = []
    for role, cap in sorted(spending.caps.items()):
        by_day = spending.per_role_day.get(role) or {}
        busiest = max(by_day.values()) if by_day else 0.0
        hit = sum(1 for spend in by_day.values() if spend >= cap * at_cap)
        if hit >= days_to_raise:
            out.append(Change(
                role=role,
                direction="raise",
                current_usd=cap,
                proposed_usd=cap * raise_by,
                days_at_cap=hit,
                busiest_day_usd=busiest,
                days_seen=len(by_day),
            ))
            continue
        # **Rule 1 at ROLE level, and it is the count of days that carries it.**
        # A role with no rows did not come near its ceiling because it did not
        # run, and a role with one row is the same statement made slightly
        # louder: its busiest day is its only day, and twice that is a cap
        # computed from a single morning. The window's own day count cannot
        # stand in for this, because it is the union across every role.
        #
        # The raise branch above needs no such floor: it already requires the
        # cap to have been reached on `days_at_cap_to_raise` separate days OF
        # THIS ROLE, which is the same guarantee arrived at from the other
        # side.
        if len(by_day) < min_days:
            continue
        if busiest <= cap * idle:
            proposed = busiest * headroom
            if proposed <= 0 or proposed >= cap:
                # Nothing to say. A cap already at or below what the headroom
                # would ask for is not too high, and a proposal of zero is
                # refused by the seam anyway.
                continue
            out.append(Change(
                role=role,
                direction="lower",
                current_usd=cap,
                proposed_usd=proposed,
                days_at_cap=0,
                busiest_day_usd=busiest,
                days_seen=len(by_day),
            ))
    return out


def _explain(
    changes: list[Change],
    spending: Spending,
    window_days: int,
) -> list[dict[str, Any]]:
    """The whole of what the card says, written here.

    **No explanatory copy about a proposal lives in TSX.** The pane renders
    these titles and lines and authors nothing, so a threshold changed in
    config cannot leave a sentence in a component describing the old one.
    """
    sections: list[dict[str, Any]] = []
    raising = [c for c in changes if c.direction == "raise"]
    lowering = [c for c in changes if c.direction == "lower"]
    # Every line counts THIS ROLE's own days, never the window's. The window
    # count is the union across every role, so quoting it beside one role's
    # number offers evidence the number does not have.
    if raising:
        sections.append({
            "title": "Raise these ceilings",
            "lines": [
                f"{c.role}: reached its {c.current_usd:.2f} dollar cap on "
                f"{c.days_at_cap} of the {c.days_seen} days it ran, busiest "
                f"day {c.busiest_day_usd:.2f}. Work is being turned away. "
                f"Proposed: {c.proposed_usd:.2f}."
                for c in raising
            ],
        })
    if lowering:
        sections.append({
            "title": "Lower these ceilings",
            "lines": [
                f"{c.role}: busiest of the {c.days_seen} days it ran was "
                f"{c.busiest_day_usd:.2f} against a {c.current_usd:.2f} dollar "
                f"cap, so the cap is not holding anything back. Proposed: "
                f"{c.proposed_usd:.2f}."
                for c in lowering
            ],
        })
    declared = proposals.kind("ceiling")
    sections.append({
        "title": "Where this comes from",
        "lines": [
            f"Read from {spending.days_seen} days of the last {window_days} of "
            "what the app has spent.",
            f"Saying yes changes {declared.moves}.",
        ],
    })
    return sections


async def _file_card(
    ctx: JobContext,
    queue: Any,
    changes: list[Change],
    spending: Spending,
    window_days: int,
) -> bool:
    from tesseract.workspace_events import WorkspaceEvent

    declared = proposals.filed("ceiling")
    count = len(changes)
    event = WorkspaceEvent.new(
        kind=CARD_KIND,
        source="agent",
        title=(
            "1 spending ceiling to change"
            if count == 1
            else f"{count} spending ceilings to change"
        ),
        summary=declared.summary,
        payload={
            "kind": declared.key,
            "changes": [c.as_json() for c in changes],
            "explain": _explain(changes, spending, window_days),
            "window_days": window_days,
            "days_seen": spending.days_seen,
            "computed_at": ctx.fired_at.isoformat(),
        },
    )
    try:
        queue.append_event(event)
    except Exception:
        log.exception("runtime_tuning: append card failed")
        return False
    await _card.announce(ctx, event, who="runtime_tuning")
    return True


__all__ = ["RuntimeTuningJob", "CARD_KIND"]
