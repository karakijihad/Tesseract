"""What a row on the panel WANTS from the operator, as against what it is.

`liveness.py::OperationalState` answers what a thing is doing. This answers the
only question a colour on an operational surface may encode: is anything asked
of the person reading it. The two are separate axes on purpose, and the reason
is measured rather than argued.

On 2026-09-04, on a machine where nothing was wrong, the panel's Needs action
band held four rows. All four were one restart, and two of them said in their
own words that the condition had ended. They were genuinely `degraded` and the
state vocabulary was right about every one of them; what was wrong was that
being degraded had been made to mean "do something". No state vocabulary can
fix that, because a finished incident IS a real state, so the axis that fixes
it is a second one.

**Colour reads this and nothing else.** A room that decides for itself what red
means is the eight-roll-ups defect: eight rooms each solved their own case and
the panel was amber anyway. There is one derivation here, and every row of
every room goes through it.

**A fact about the code is not an obligation.** `not_instrumented` keeps its own
rendering, because "nothing produces this" is not a demand, not a fault, and
not a state of health. So does a capability turned off, and one never set up:
`views/settings/Capabilities.tsx` already draws those in ordinary text and the
panel does not get to paint them as faults.
"""

from __future__ import annotations

from enum import Enum

from tesseract.orchestrator.liveness import OperationalState


class Obligation(str, Enum):
    """What the row is asking of whoever is reading it."""

    STANDING_FAULT = "standing_fault"
    """Broken now, and it will stay broken until somebody acts."""

    NEEDS_YOU = "needs_you"
    """Working below what it promised, or waiting on a decision only the
    operator can make."""

    BEING_HEALED = "being_healed"
    """Something is wrong and the runtime is already working on it. Worth
    knowing about; nothing is asked. AR-28 is what produces this."""

    HAPPENED_OVER = "happened_over"
    """It went wrong, it is over, and the record is kept. Carries its clock,
    because a fact with no time cannot be told from a condition."""

    OFF_BY_CHOICE = "off_by_choice"
    """Somebody turned it off. Nothing is expected of it and nothing is wrong."""

    NEVER_SET_UP = "never_set_up"
    """It exists and has never been configured. A choice not yet made is not a
    fault."""

    NOTHING_WATCHES = "nothing_watches"
    """No producer exists in the code. Rendered as the unwired state and never
    as quiet, which is `not_instrumented`'s own rule."""

    FINE = "fine"
    """Checked, and nothing is asked. Green is reachable, which for seven of
    the rail's eight rows it was not."""


# What each obligation is called on a surface. The enum's names are for the
# code; a person reading a room needs words, and they are written here so a
# value added later has to be given them in the same pass.
LABELS: dict[Obligation, str] = {
    Obligation.STANDING_FAULT: "needs fixing",
    Obligation.NEEDS_YOU: "needs you",
    Obligation.BEING_HEALED: "it is working on it",
    Obligation.HAPPENED_OVER: "happened, and is over",
    Obligation.OFF_BY_CHOICE: "off, because you turned it off",
    Obligation.NEVER_SET_UP: "never set up",
    Obligation.NOTHING_WATCHES: "no runtime signal",
    Obligation.FINE: "fine",
}


# Worst first. One order, so "what is the worst thing in this room" is answered
# in one place for the rail, the bands and every future reader.
_SEVERITY: dict[Obligation, int] = {
    Obligation.STANDING_FAULT: 7,
    Obligation.NEEDS_YOU: 6,
    Obligation.BEING_HEALED: 5,
    Obligation.NOTHING_WATCHES: 4,
    Obligation.HAPPENED_OVER: 3,
    Obligation.NEVER_SET_UP: 2,
    Obligation.OFF_BY_CHOICE: 1,
    Obligation.FINE: 0,
}


# What a state means when nothing else is known about the row. Written as a
# table rather than a chain of ifs so a state added to `OperationalState` has
# to be given a meaning here, and a test fails until it is.
_FROM_STATE: dict[OperationalState, Obligation] = {
    OperationalState.FAILED: Obligation.STANDING_FAULT,
    OperationalState.DEGRADED: Obligation.NEEDS_YOU,
    # It deliberately did not begin. Whether that is a choice or a fault is the
    # caller's to say with `by_choice`; unqualified, somebody should know.
    OperationalState.REFUSED: Obligation.NEEDS_YOU,
    # Something is watching and could not report. That is not the same as
    # nothing watching, and the room is not well while it is true.
    OperationalState.UNKNOWN: Obligation.NEEDS_YOU,
    OperationalState.NOT_INSTRUMENTED: Obligation.NOTHING_WATCHES,
    OperationalState.RUNNING: Obligation.FINE,
    OperationalState.IDLE: Obligation.FINE,
    OperationalState.PENDING: Obligation.FINE,
}


def obligation_of(
    state: OperationalState,
    *,
    unconfigured: bool = False,
    by_choice: bool = False,
    acknowledged: bool = False,
    awaiting_operator: bool = False,
    healing: bool = False,
    ended: bool = False,
) -> Obligation:
    """What this row asks for, from its state and what else is known about it.

    The order the qualifiers are read in is the argument for each of them:

    - **Never set up** outranks everything, because a thing nobody has
      configured cannot be failing at what it was never asked to do.
    - **Off by choice** is next: a row somebody turned off is expected to be
      doing nothing, and a rail that went amber every time something was turned
      off would teach the operator to stop reading the colour.
    - **Acknowledged** is the operator's own word about it, and it outranks the
      runtime's reading. The state stays exactly what it is; only the demand
      goes away. The row keeps saying what is true and adds who left it and
      when, which is `acknowledged.py`'s first rule.
    - **Awaiting the operator** is the one fact a state cannot carry. `pending`
      means work is expected and has not started, which for a stage inside an
      open run asks for nothing and for an agent card waiting to be approved is
      the whole of what it asks. The row knows which it is; the state does not.
    - **Being healed** before **ended**, because something the runtime is
      working on right now is not over.
    - Otherwise the state decides.
    """
    if unconfigured:
        return Obligation.NEVER_SET_UP
    if by_choice:
        return Obligation.OFF_BY_CHOICE
    if acknowledged:
        return Obligation.FINE
    if awaiting_operator:
        return Obligation.NEEDS_YOU
    if healing:
        return Obligation.BEING_HEALED
    if ended:
        return Obligation.HAPPENED_OVER
    try:
        return _FROM_STATE[state]
    except KeyError:  # pragma: no cover - the enum is closed and covered by a test
        raise ValueError(
            f"state {state.value!r} says nothing about what it wants. Add it to "
            "_FROM_STATE and give it a rendering in the same pass"
        ) from None


def label_of(obligation: Obligation) -> str:
    """What to call this on a surface."""
    try:
        return LABELS[obligation]
    except KeyError:  # pragma: no cover - closed enum, covered by a test
        raise ValueError(
            f"obligation {obligation.value!r} has no words. Give it a line in "
            "LABELS: a surface cannot print a name only this file understands"
        ) from None


def as_payload(
    state: OperationalState,
    *,
    unconfigured: bool = False,
    by_choice: bool = False,
    acknowledged: bool = False,
    awaiting_operator: bool = False,
    healing: bool = False,
    ended: bool = False,
) -> dict[str, str]:
    """The two fields every row on the panel carries, ready to splat.

    The rooms build their rows as dictionaries in eight different places, and
    the point of this axis is that the derivation happens once. A helper the
    row spreads is what makes that true without rewriting every builder into
    one constructor, which would be a larger change with more room to drift.
    """
    wants = obligation_of(
        state,
        unconfigured=unconfigured,
        by_choice=by_choice,
        acknowledged=acknowledged,
        awaiting_operator=awaiting_operator,
        healing=healing,
        ended=ended,
    )
    return {"obligation": wants.value, "obligationLabel": label_of(wants)}


def worst(obligations: "list[Obligation] | tuple[Obligation, ...]") -> Obligation:
    """The one a room wears, which is the worst thing in it.

    An empty room is `fine`. It has nothing wrong with it, which is a different
    claim from having no producer, and the caller that means the second one
    passes `NOTHING_WATCHES` rather than nothing.
    """
    return max(obligations, key=lambda o: _SEVERITY[o], default=Obligation.FINE)


def severity_of(obligation: Obligation) -> int:
    """Where this sits in the one order. For a caller that ranks rows itself."""
    return _SEVERITY[obligation]


__all__ = [
    "LABELS",
    "Obligation",
    "as_payload",
    "label_of",
    "obligation_of",
    "severity_of",
    "worst",
]
