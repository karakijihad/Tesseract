"""How something reads right now, as against what a finished run produced.

``RunOutcome`` answers what came of a run that ended. This answers what an
operator sees on the Autonomy panel: a stage still waiting its turn, a
department whose producer does not exist, a socket that went stale. The last
two are the reason the vocabulary exists at all — a pane that renders "nothing
is producing this" the same as "nothing is happening" answers a question it
cannot answer, which is how five signals came to sit permanently cold.

The set is closed. A state added here needs a rendering in the same pass, and
`tests/autonomy_rewrite_AR_8/test_the_states_the_view_can_render.py` fails until
it has one.
"""

from __future__ import annotations

from enum import Enum

from tesseract.orchestrator.outcome import RunOutcome


class OperationalState(str, Enum):
    RUNNING = "running"
    """Started, and beating within its declared interval."""

    IDLE = "idle"
    """Instrumented, healthy, nothing expected of it right now. Carries either
    its next eligible run or its last completed one."""

    PENDING = "pending"
    """Work IS expected and has not started — a stage inside an open run that
    has not had its turn. Distinct from `idle`, which says the opposite."""

    DEGRADED = "degraded"
    """Produced something, below its declared contract. Also where a `running`
    department lands when its heartbeat goes overdue."""

    FAILED = "failed"
    """Tried and errored."""

    REFUSED = "refused"
    """Deliberately did not begin: policy, a pause, an open breaker, a missing
    capability, an unsatisfied dependency."""

    NOT_INSTRUMENTED = "not_instrumented"
    """No producer exists in the code. Never rendered as quiet and never green."""

    UNKNOWN = "unknown"
    """The socket is stale or a sequence gap was seen. The prior state is never
    reused to fill the gap."""


# One outcome maps to one state, and the two vocabularies stay separate on
# purpose: `truncated` and `skipped_upstream_failed` are things that happened to
# a run, while what an operator needs from the strip is whether the step is fine,
# working below contract, or did not happen. Collapsing them here rather than in
# the view is what keeps the mapping testable.
_FROM_OUTCOME: dict[RunOutcome, OperationalState] = {
    RunOutcome.SUCCEEDED: OperationalState.IDLE,
    RunOutcome.SKIPPED_NO_WORK: OperationalState.IDLE,
    RunOutcome.DEGRADED: OperationalState.DEGRADED,
    # It hit its budget and left the rest for the next anchor. Something is
    # outstanding, so this is not the plain "done" that `succeeded` is.
    RunOutcome.TRUNCATED: OperationalState.DEGRADED,
    RunOutcome.FAILED: OperationalState.FAILED,
    RunOutcome.REFUSED: OperationalState.REFUSED,
    # It never began, because what it reads did not succeed. That is a refusal
    # by the runner rather than a failure of this stage.
    RunOutcome.SKIPPED_UPSTREAM_FAILED: OperationalState.REFUSED,
}


# What each state is called on a surface. The enum's names are for the code and
# two of them are unreadable to anybody who has not read it: a person looking at
# a map needs "no runtime signal", not `not_instrumented`. Written here so one
# vocabulary reaches every surface, and so a state added later has to be given
# words in the same pass rather than leaking its slug onto a screen.
LABELS: dict[OperationalState, str] = {
    OperationalState.RUNNING: "working",
    OperationalState.IDLE: "quiet",
    OperationalState.PENDING: "waiting its turn",
    OperationalState.DEGRADED: "working, below what it promised",
    OperationalState.FAILED: "it failed",
    OperationalState.REFUSED: "it did not start",
    OperationalState.NOT_INSTRUMENTED: "no runtime signal",
    OperationalState.UNKNOWN: "not known",
}


def label_of(state: OperationalState) -> str:
    """What to call this state on a surface."""
    try:
        return LABELS[state]
    except KeyError:  # pragma: no cover - closed enum, covered by a test
        raise ValueError(
            f"state {state.value!r} has no words. Give it a line in LABELS — a "
            "surface cannot print a name only this file understands"
        ) from None


def state_of(outcome: RunOutcome) -> OperationalState:
    """The state a committed run record reads as."""
    try:
        return _FROM_OUTCOME[outcome]
    except KeyError:  # pragma: no cover - the enum is closed and covered by a test
        raise ValueError(
            f"outcome {outcome.value!r} has no operational state. Add it to "
            "_FROM_OUTCOME and give it a rendering in the same pass"
        ) from None


def labels_payload() -> dict[str, str]:
    """Every state's word, for a surface that has to render one nothing sent it.

    A panel that has stopped hearing from the runtime draws what it is still
    entitled to claim, which is `unknown`, and it has to have the word for it
    without the backend being reachable to supply it. Shipping the whole table
    with the payload keeps that surface on the same vocabulary as every other:
    a word typed into TSX would be the second place a state is named, and the
    first one to drift.
    """
    return {state.value: LABELS[state] for state in OperationalState}


__all__ = ["LABELS", "OperationalState", "label_of", "labels_payload", "state_of"]
