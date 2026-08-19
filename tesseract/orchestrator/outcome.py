"""The closed result vocabulary for a unit of background work.

Lifecycle status answers "where is it"; outcome answers "what came of it".
Collapsing the two is how eight autonomy workers that returned no text at all
were persisted as ``done``: the runner asked only whether the tool call raised,
and an empty string does not raise.

Shared by the worker runner and the scheduler so a run reads the same either
side of the boundary. Adding a state means adding it here, to whatever
persists it, and to the surface that renders it — in the same pass.
"""

from __future__ import annotations

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

    SKIPPED_UPSTREAM_FAILED = "skipped_upstream_failed"
    """A declared input dependency did not succeed."""


# Neither of these is a defect: one produced work, the other correctly
# found none. Everything else needs a reason and belongs in a health count.
HEALTHY_OUTCOMES: frozenset[RunOutcome] = frozenset(
    {RunOutcome.SUCCEEDED, RunOutcome.SKIPPED_NO_WORK}
)

# Only these ran far enough to have been counted as an attempt that could
# have produced something — the denominator of a completion rate.
ATTEMPTED_OUTCOMES: frozenset[RunOutcome] = frozenset(
    {
        RunOutcome.SUCCEEDED,
        RunOutcome.SKIPPED_NO_WORK,
        RunOutcome.DEGRADED,
        RunOutcome.TRUNCATED,
        RunOutcome.FAILED,
    }
)


def outcome_from_ok(ok: bool) -> RunOutcome:
    """The old boolean, widened. Only for callers that have not yet
    declared an outcome of their own — it cannot tell `skipped_no_work`
    or `refused` apart from the two states it does return, which is the
    whole reason the vocabulary exists."""
    return RunOutcome.SUCCEEDED if ok else RunOutcome.FAILED


__all__ = [
    "ATTEMPTED_OUTCOMES",
    "HEALTHY_OUTCOMES",
    "RunOutcome",
    "outcome_from_ok",
]
