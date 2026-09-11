"""The closed result vocabulary for a unit of background work.

Lifecycle status answers "where is it"; outcome answers "what came of it".
Collapsing the two is how eight autonomy workers that returned no text at all
were persisted as ``done``: the runner asked only whether the tool call raised,
and an empty string does not raise.

Shared by the worker runner and the scheduler so a run reads the same either
side of the boundary. Adding a state means adding it here, to whatever
persists it, and to the surface that renders it, in the same pass.

**Every place a member has to be added, held in one list.** Two of these
fail loudly and one fails silently, which is why the quiet one is named
first rather than left to be discovered:

  1. ``SEVERITY`` below — SILENT. ``worst_of`` walks the tuple and returns
     ``SUCCEEDED`` when it falls off the end, so a member missing there
     does not raise, it reports a bad run as a good one.
  2. ``liveness.py::_FROM_OUTCOME`` — raises, and a parametrized test over
     ``list(RunOutcome)`` covers it.
  3. ``scripts/generate_guide.py::OUTCOME_BLURBS`` — raises at generation.
  4. ``scheduler/log.py::_severity_of`` — falls through to ``INFO``, which
     is right for a healthy state and wrong for anything else.
  5. ``autonomy/kernel_worker_runner.py::_DONE_OUTCOMES`` — omission means
     the worker reads ``FAILED``, which is the safe direction.
  6. ``HEALTHY_OUTCOMES`` / ``ATTEMPTED_OUTCOMES`` below.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum


class RunOutcome(str, Enum):
    """Exactly one of these resolves every run, and the set is closed.

    A run that ends any other way is a bug in whatever ended it, not a fifth
    outcome — the members below carry the distinctions that exist."""

    SUCCEEDED = "succeeded"
    """Ran, produced output that passed its own validation."""

    SKIPPED_NO_WORK = "skipped_no_work"
    """Ran, there was nothing to do. Healthy, and says so."""

    REFUSED = "refused"
    """Deliberately did not begin: policy, operator pause, breaker open,
    missing capability, dependency unsatisfied, lock held."""

    DEGRADED = "degraded"
    """Produced output below its declared contract — fallback path,
    partial result."""

    TRUNCATED = "truncated"
    """Hit its wallclock budget; the remainder resumes from its watermark."""

    FAILED = "failed"
    """Tried and errored."""

    CALLER_ERROR = "caller_error"
    """Ran correctly and was asked for something it cannot do: a path that is
    not there, an argument of the wrong shape, work that is already done.

    Wrong is not the same as broken, and one word for both made the most
    used tool in the registry look like the least reliable one. On one
    measured day `file_read` was called 87 times and 2 came back "File not
    found", which says the model asked for a file that does not exist and
    says nothing at all about reading files.

    Never inferred. The tool sets `ToolResult.caller_error` at the point it
    already knows, and the default is False, so a tool nobody has visited
    keeps reading `failed` exactly as it does today. `depends_on` cannot
    answer this and must not be used to: it declares which breaker a tool
    is counted against, so `channel_send` carries `""` and a Telegram
    outage would have been filed as the model's mistake."""

    UNVERIFIED = "unverified"
    """Acted, did not error, and left nothing anybody can go and check.

    Not a success and not a failure, and it is counted in every denominator
    rather than folded into the green. A tool that declares a receipt kind
    and returns no receipt lands here: something happened outside this
    process and the only account of it is the actor's own."""

    SKIPPED_UPSTREAM_FAILED = "skipped_upstream_failed"
    """A declared input dependency did not succeed."""


# Neither of these is a defect: one produced work, the other correctly
# found none. Everything else needs a reason and belongs in a health count.
HEALTHY_OUTCOMES: frozenset[RunOutcome] = frozenset(
    {RunOutcome.SUCCEEDED, RunOutcome.SKIPPED_NO_WORK}
)

# Only these ran far enough to have been counted as an attempt that could
# have produced something — the denominator of a completion rate.
#
# `unverified` is in here deliberately. A metric that drops it is reporting
# on the cases that happened to be checkable, which is the denominator this
# whole vocabulary exists to keep honest. `caller_error` is in here for the
# plainer reason that it ran.
ATTEMPTED_OUTCOMES: frozenset[RunOutcome] = frozenset(
    {
        RunOutcome.SUCCEEDED,
        RunOutcome.SKIPPED_NO_WORK,
        RunOutcome.DEGRADED,
        RunOutcome.TRUNCATED,
        RunOutcome.FAILED,
        RunOutcome.CALLER_ERROR,
        RunOutcome.UNVERIFIED,
    }
)


# Worst first. What one row, turn or day is CALLED when it holds several
# outcomes, and it lives here because the ordering is a property of the
# vocabulary rather than of any one reader. The scheduler had the only copy
# while it was the only caller; the day reader is a second, and two orderings
# would let a panel and a row disagree about which of two bad things is worse.
#
# `skipped_no_work` sits above `succeeded` deliberately: a row where one stage
# found nothing and another did real work has succeeded, and only a row where
# NOTHING was due reports that it had nothing to do.
#
# **Every member has to be here.** `worst_of` falls back to `succeeded` when it
# finds no match, so a state left out does not raise, it reports a bad run as a
# good one.
SEVERITY: tuple[RunOutcome, ...] = (
    RunOutcome.FAILED,
    RunOutcome.SKIPPED_UPSTREAM_FAILED,
    RunOutcome.DEGRADED,
    # Above `truncated`: a run that stopped short still says what it did, and
    # this one cannot.
    RunOutcome.UNVERIFIED,
    RunOutcome.TRUNCATED,
    # Above `refused`, which is a deliberate and healthy stop, and above
    # `succeeded`, because a run where one call was asked wrong has more to say
    # than that it worked.
    RunOutcome.CALLER_ERROR,
    RunOutcome.REFUSED,
    RunOutcome.SUCCEEDED,
    RunOutcome.SKIPPED_NO_WORK,
)


def worst_of(outcomes: "Iterable[RunOutcome]") -> RunOutcome:
    """What to call a group of runs, by the worst thing in it.

    An empty group is `skipped_no_work`: nothing ran, and reporting that as a
    success would be the collapse this vocabulary exists to prevent.
    """
    held = set(outcomes)
    if not held:
        return RunOutcome.SKIPPED_NO_WORK
    for candidate in SEVERITY:
        if candidate in held:
            return candidate
    return RunOutcome.SUCCEEDED


def outcome_from_ok(ok: bool) -> RunOutcome:
    """The old boolean, widened. Only for callers that have not yet
    declared an outcome of their own — it cannot tell `skipped_no_work`
    or `refused` apart from the two states it does return, which is the
    whole reason the vocabulary exists."""
    return RunOutcome.SUCCEEDED if ok else RunOutcome.FAILED


__all__ = [
    "ATTEMPTED_OUTCOMES",
    "HEALTHY_OUTCOMES",
    "SEVERITY",
    "RunOutcome",
    "outcome_from_ok",
    "worst_of",
]
