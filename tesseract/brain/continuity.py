"""The package a cleared conversation carries on with.

A `continue` says the work goes on and this conversation has stopped serving
it. So the boundary reflects, archives what was said, and clears the thread in
place. What the next turn then has to go on is this: the checkpoint the
reflection wrote, rendered as the conversation's first message.

Operator, 2026-09-06: *"its basically task lists, what was done, what is
remaining. its like a person who slept in the night and continued working in
the morning, he picks up the task."*

**A message, not a prompt block.** A block would be invisible to the operator
and re-sent every turn forever. This is written once, is visible in the cockpit
and on a channel, and is part of the record from then on. It is also where the
cache wants it: the head does not move across the boundary, so the package
lands immediately after a prefix the provider still has, is paid for once, and
rides inside that prefix from the next turn on.

**References, never copies**, which costs nothing to hold to because the record
already enforces it: `artifacts` holds paths and ids, and no field holds a body.

**It is built at the boundary, from the handoff the agent just wrote.** It used
to be built when the reflection finished, because the reflection was what wrote
the record, and that put a model turn between the conversation being cleared
and anything being able to carry on: thirty-four seconds, measured on the
operator's own run. It also listed what that reflection had saved to memory,
and waiting for that list was the whole of the delay. What reflection saves
reaches the operator through the inbox, and the turn that carries the work on
finds the memories with `memory_search`.

**No second bound, and nothing is summarised.** The record is bounded where it
is written, at `checkpoints.LIST_CAP` items a list and `checkpoints.FIELD_CHARS`
a field. A cap here as well would be a second answer to a question already
settled, and what it would cut is exactly what §2 of the owner's document says
must not be cut. The conversation itself survives too: it is archived and still
searchable through `recall_history`, so nothing on this path is anybody's only
copy and the summary-of-a-summary failure mode cannot arise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import yaml

from tesseract.orchestrator import checkpoints
from tesseract.orchestrator.checkpoints import Checkpoint
from tesseract.paths import config_dir

log = logging.getLogger(__name__)

#: What the message opens with. Addressed to the model, and read by the
#: operator over its shoulder, which is why it says what happened rather than
#: naming the mechanism that did it.
LEAD = (
    "This conversation was consolidated and cleared, and the work carries on "
    "here. What was said is archived and searchable with `recall_history`; "
    "what it taught is in memory. This is where the work stood."
)

#: The sections, in the order a person would ask them. Each is `(heading,
#: attribute)`; a list attribute renders as bullets and a string as a line.
_FIELDS: tuple[tuple[str, str], ...] = (
    ("Objective", "objective"),
    ("Phase", "phase"),
    ("Done", "completed"),
    ("Remaining", "remaining"),
    ("Next", "next_action"),
    ("Open questions", "open_questions"),
    ("Blocked by", "blocked_by"),
    # Immediately before the paths it makes readable. A list of relative paths
    # with nothing saying what they hang off is a list the next context cannot
    # open, and it goes looking for the project it is already standing in.
    ("Working from", "work_root"),
    ("Artifacts", "artifacts"),
)


def _blocks(checkpoint: Checkpoint) -> list[str]:
    """Every field that has anything in it, rendered. Nothing is left out.

    A field the boundary left empty gets no heading rather than an empty one:
    the model reads this, and a heading with nothing under it is a question it
    cannot answer and will try to.
    """
    out: list[str] = []
    for heading, attribute in _FIELDS:
        value = getattr(checkpoint, attribute)
        if not value:
            continue
        if isinstance(value, list):
            out.append("\n".join([f"{heading}:", *(f"- {item}" for item in value)]))
        else:
            out.append(f"{heading}: {value}")
    return out


def render(checkpoint: Checkpoint) -> str:
    """The package, or `""` when the boundary had nothing to say at all.

    An empty checkpoint is a real answer and not a failure: a conversation
    that was never about a piece of work leaves one, and opening its successor
    with a heading and no content would be worse than opening it with nothing.
    """
    if checkpoint.is_empty():
        return ""
    return "\n\n".join([LEAD, *_blocks(checkpoint)])


def package_for(checkpoint: Checkpoint | None) -> str:
    """The package for one boundary, or `""` when there is none to give.

    `None` is a boundary with no handoff behind it, which is the conversation
    that was asked for one and never gave it. There is nothing to hand over
    and nothing to invent, so it hands over nothing.

    Fails to nothing rather than raising. By the time this runs the boundary
    has already archived and cleared the conversation, and a package that
    cannot be built must not leave the person wondering what happened to their
    thread.
    """
    if checkpoint is None:
        return ""
    return render(checkpoint)




# ── Whether a boundary may hand anything over at all ────────────────
#
# "Continuation must have a reason", and there is exactly one now: the handoff
# the agent wrote at THIS boundary has to say there is something left to do.
#
# It used to read the boundaries the conversation had already crossed, because
# the record was written afterwards by a reflection and this boundary's did not
# exist yet. Three rules lived there: nothing was owed last time, the same next
# action came back, and a window of boundaries introduced nothing new. Only the
# first survives, and it has moved onto the record in front of it rather than
# the one behind.
#
# The other two are deleted rather than tuned, on the operator's ruling that
# reflection is an independent system with no weight on the decision. Both
# compared SENTENCES the agent now writes itself, so rephrasing the next action
# walked past either one, and `OPERATING.md` was teaching exactly that. A rule
# the thing it judges can step over for free is worse than no rule: it
# advertises a bound that is not there.
#
# What bounds a conversation that keeps writing real remaining items is what
# bounds anything else that runs, which is the budget, and the runtime cutting
# off a conversation that will not answer a hard boundary at all.


@dataclass(frozen=True)
class BoundaryBounds:
    reflection_ceiling_seconds: float


def _require(d: dict, key: str, where: str):
    if key not in d:
        raise RuntimeError(f"missing required key '{key}' in {where}")
    return d[key]


def load_boundary_bounds() -> BoundaryBounds:
    """Read ``roles.yaml::boundary``; raise loudly on a missing key.

    Beside `compaction:` and for the same reason it is there: it describes how
    a conversation is bounded rather than who is using it. Resolved at call
    time, so the config watcher's rebuild is enough to change it.
    """
    raw = yaml.safe_load((config_dir() / "roles.yaml").read_text(encoding="utf-8"))
    section = _require(raw, "boundary", "roles.yaml")
    return BoundaryBounds(
        reflection_ceiling_seconds=float(
            _require(section, "reflection_ceiling_seconds", "roles.yaml boundary")
        ),
    )


def why_not_continue(handoff: Checkpoint | None) -> str:
    """Why this conversation may not carry the work on, or `""`.

    One refusal, read off the handoff the agent wrote for THIS boundary:
    it reported nothing remaining and no next action, so there is nothing for
    the next turn to pick up.

    **That is an answer and not an omission**, which is why nothing asks again.
    A handoff with neither field is the agent saying the work is finished, and
    the right act is to leave the conversation behind. Asking it to reconsider
    would make inventing a remaining item the cheapest way past the gate, which
    is the evasion this rule is supposed to be free of.

    `None` is a boundary with no handoff at all, which is a different event:
    the conversation was asked and did not answer. It is refused here too, and
    `after_turn` is what decides that asking again is not worth it.

    A refusal is a sentence, because it is written onto the record and read by
    a person. Empty means the continue stands.
    """
    if handoff is None:
        return (
            "this conversation was asked where the work stood and did not "
            "say, so there is nothing to carry on with"
        )
    if not handoff.next_action and not handoff.remaining:
        return (
            "you reported nothing left to do and nothing to do next, so there "
            "is nothing for this conversation to carry on with"
        )
    return ""


__all__ = [
    "LEAD",
    "BoundaryBounds",
    "load_boundary_bounds",
    "render",
    "package_for",
    "why_not_continue",
]
