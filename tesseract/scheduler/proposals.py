"""What the runtime is allowed to propose about itself, declared in one place.

A tuning proposal is a card: the runtime reads its own gauges, says what it
would change and what number it read, and the operator answers. Nothing here
applies anything. The seam each kind names is a tool or a route that already
asks, so the operator's yes is the call and the runtime's part ends at the
card.

**The list is closed, and that is the point.** Two jobs file these cards today
and they fire on different evidence, so without one declaration the vocabulary
would be whatever each producer happened to write into a payload. A kind that
is not here cannot be filed. Adding one means writing its evidence rule and
its seam in the same pass, which is the guard against a proposal kind that
sounds reasonable and has nothing behind it.

**A kind with no producer is listed rather than omitted**, the same as
`spent.MEASURES`: the card can then say the number does not exist yet, which
is a different answer from zero and the only honest one.
"""

from __future__ import annotations

from dataclasses import dataclass


class UnknownProposalKind(ValueError):
    """A producer tried to file a kind this file does not declare."""


@dataclass(frozen=True)
class ProposalKind:
    #: Stable id. It is what the card's payload carries, so renaming one
    #: orphans the cards already filed under the old name.
    key: str
    #: What it proposes, in the operator's language.
    summary: str
    #: What goes wrong without it. The field that makes a kind's keep or drop
    #: checkable, the same contract the tool and manifest entries carry.
    why: str
    #: The tool, file or route the operator's yes moves. Every one of them
    #: asks already, so naming it here widens nothing. **Machine facing**: it
    #: is what the code and the tests name, and it never reaches a screen.
    seam: str
    #: The same thing in the operator's words, for the card. A route and a
    #: yaml key path are exactly the identifiers the copy rule bars, and the
    #: one sentence saying what a yes will do is the worst place for them.
    moves: str
    #: The records the evidence is read from, so a claim on a card can be
    #: checked against the same file the panel reads.
    reads: tuple[str, ...]
    #: False when nothing on this machine records what the kind would need.
    #: The card says so and files no number.
    instrumented: bool = True
    #: Required when `instrumented` is False, forbidden otherwise.
    missing: str = ""


#: The closed set. Two producers draw from it: `working_set_review` files the
#: first two, `runtime_tuning` the ceiling. The last two are declared and not
#: fileable, which is the honest state for a kind whose number does not exist.
#:
#: **A kind is removed when its SEAM will not take it**, which is a different
#: failure from having no gauge and has no honest `missing` sentence.
#: `run_less_often` was here and is gone: it proposed re-timing a scheduled
#: row, and `config_loader.py::refuse_cadence` refuses any change to how often
#: a SHIPPED row runs, on the rule that the app owns the timing of its own
#: work. Every row a user has is shipped, so the kind could only ever file a
#: card whose approval was refused. It is not re-scoped to "turn the row off"
#: either: a row producing the same thing every time is often a quiet watchman
#: doing its job, and proposing to stop it is a different and worse claim.
KINDS: tuple[ProposalKind, ...] = (
    ProposalKind(
        key="carry_more",
        summary="Carry a tool or a playbook that every turn keeps looking up first.",
        why=(
            "Reaching for something through a search costs a call before the "
            "work starts. Without this, a tool used constantly stays off the "
            "list and is paid for twice on every turn that wants it."
        ),
        seam="config/working_set.yaml::core and workspace/skills/carried.txt",
        moves="what every turn carries",
        reads=("logs/usage/tools.jsonl", "logs/skills/usage.jsonl"),
    ),
    ProposalKind(
        key="carry_less",
        summary="Stop carrying a tool or a playbook nothing has reached for.",
        why=(
            "What a turn carries is roughly half of what it costs before you "
            "have typed anything. Without this, the list only ever grows."
        ),
        seam="config/working_set.yaml::core and workspace/skills/carried.txt",
        moves="what every turn carries",
        reads=("logs/usage/tools.jsonl", "logs/skills/usage.jsonl"),
    ),
    ProposalKind(
        key="ceiling",
        summary=(
            "Raise a role's daily spending cap when it keeps hitting it, or "
            "lower one it never comes close to."
        ),
        why=(
            "A cap that is hit every day turns work away, and one set ten "
            "times too high is not a cap. Without this, both look the same "
            "from the outside, which is quiet."
        ),
        seam="POST /api/settings/cost",
        moves="the daily spending limits named above",
        reads=("logs/cost-tracking.jsonl", "config/roles.yaml"),
    ),
    ProposalKind(
        key="prefer_the_playbook",
        summary=(
            "Carry a playbook for a kind of task that keeps being worked "
            "without it."
        ),
        why=(
            "A procedure written down and never reached for is the same as "
            "not having written it. Without this, the library grows and the "
            "work does not get easier."
        ),
        seam="workspace/skills/carried.txt",
        moves="which playbooks every turn carries",
        reads=(),
        instrumented=False,
        missing=(
            "The playbook log records which ones ran, never which piece of "
            "work ran without one, so nothing here can say a playbook was "
            "the one that should have been reached for."
        ),
    ),
    ProposalKind(
        key="keep_less",
        summary=(
            "Shorten how long a tree is kept when nothing has read it since "
            "it aged past its window."
        ),
        why=(
            "Keeping everything forever is a decision nobody made. Without "
            "this, a retention window set once is never questioned."
        ),
        seam="retention_set_window",
        moves="how long that is kept",
        reads=(),
        instrumented=False,
        missing=(
            "Nothing records when a kept tree was last read, so this cannot "
            "say whether anything still wants it."
        ),
    ),
)


def _validate() -> dict[str, ProposalKind]:
    """Raise at import rather than at the moment a card is filed.

    A kind missing its `why` is only discoverable when the operator opens the
    card, which is hours after the run that wrote it and on the one surface
    where an unanswerable card costs the most.
    """
    out: dict[str, ProposalKind] = {}
    for declared in KINDS:
        for field in ("key", "summary", "why", "seam", "moves"):
            if not getattr(declared, field).strip():
                raise ValueError(
                    f"proposal kind {declared.key!r} declares no {field}: every "
                    f"kind says what it proposes, what goes wrong without it, "
                    f"the seam the operator's answer moves, and that seam in "
                    f"words the operator reads"
                )
        if declared.instrumented and declared.missing:
            raise ValueError(
                f"proposal kind {declared.key!r} is instrumented and also says "
                f"what is missing: one of the two is wrong"
            )
        if not declared.instrumented and not declared.missing.strip():
            raise ValueError(
                f"proposal kind {declared.key!r} is not instrumented and does "
                f"not say what is missing: the card has to be able to say why "
                f"it has no number"
            )
        if declared.key in out:
            raise ValueError(f"proposal kind {declared.key!r} is declared twice")
        out[declared.key] = declared
    return out


_BY_KEY = _validate()


def kind(key: str) -> ProposalKind:
    """The declared kind, or an error naming the ones that exist."""
    found = _BY_KEY.get(key)
    if found is None:
        known = ", ".join(sorted(_BY_KEY))
        raise UnknownProposalKind(
            f"{key!r} is not a proposal kind this runtime declares. It knows: "
            f"{known}. Adding one means declaring its evidence and its seam in "
            f"tesseract/scheduler/proposals.py"
        )
    return found


def filed(key: str) -> ProposalKind:
    """The kind a producer is about to file, refusing an undeclared one and a
    kind whose evidence this machine does not record."""
    found = kind(key)
    if not found.instrumented:
        raise UnknownProposalKind(
            f"{key!r} is declared but cannot be filed: {found.missing}"
        )
    return found


__all__ = ["KINDS", "ProposalKind", "UnknownProposalKind", "filed", "kind"]
