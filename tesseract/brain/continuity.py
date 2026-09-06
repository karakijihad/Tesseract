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

The same rule is what makes the last section safe. The reflection writes what
the conversation TAUGHT into the memory store, and it hands back a list of what
it wrote. Naming those beside the state is the difference between "there is
something in memory about this" and knowing where: retrieval ranks on the next
question, and the note made ten minutes ago about the work still in progress is
exactly the one a general search does not rank up. So the package carries the
title and the path of each, and never the content, which `memory_get` and
`file_read` already own.

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


def _written_down(saves: list[dict] | None) -> str:
    """What the reflection put in memory, by title and by where it landed.

    Never the content: `memory_get` and `file_read` own that, and a package
    holding a copy is the drift this record's whole shape is written against.

    A save that did NOT land is left out rather than listed with its status. A
    package is what the next turn may rely on, and a line saying something was
    blocked invites it to act as though the thing is there. The failure has its
    own record in the workspace inbox, which is where the operator answers it.
    """
    if not saves:
        return ""
    lines: list[str] = []
    for save in saves:
        if not isinstance(save, dict):
            continue
        if save.get("status") not in ("saved", "completed"):
            continue
        title = str(save.get("title") or "").strip()
        if not title:
            continue
        where = str(save.get("path") or save.get("memory_id") or "").strip()
        lines.append(f"- {title}" + (f" ({where})" if where else ""))
    if not lines:
        return ""
    return "\n".join(["Written down just now:", *lines])


def render(checkpoint: Checkpoint, saves: list[dict] | None = None) -> str:
    """The package, or `""` when the boundary had nothing to say at all.

    An empty checkpoint with nothing written down is a real answer and not a
    failure: a conversation that was never about a piece of work leaves one,
    and opening its successor with a heading and no content would be worse
    than opening it with nothing.
    """
    blocks = [] if checkpoint.is_empty() else _blocks(checkpoint)
    written = _written_down(saves)
    if written:
        blocks.append(written)
    if not blocks:
        return ""
    return "\n\n".join([LEAD, *blocks])


#: A checkpoint that says nothing, so a reflection whose record did not land
#: can still hand over what it wrote to memory. Losing one is not a reason to
#: lose the other.
_NOTHING = Checkpoint(
    checkpoint_id="", ts="", session_id="", trigger="", outcome="",
)


def package_for(
    checkpoint: Checkpoint | None, saves: list[dict] | None = None
) -> str:
    """The package for one boundary, or `""` when there is none to give.

    Fails to nothing rather than raising. By the time this runs the boundary
    has already reflected, archived and cleared the conversation, and a package
    that cannot be built must not leave the person wondering what happened to
    their thread.
    """
    return render(checkpoint if checkpoint is not None else _NOTHING, saves)




# ── Whether a boundary may hand anything over at all ────────────────
#
# "Continuation must have a reason", and the runtime's existing bound is the
# wrong instrument for it: the failure breaker counts failures, and a loop that
# continues successfully and achieves nothing never trips it.
#
# The agent decides whether meaningful work remains and this does not argue
# with that. What it refuses is a continue nothing is OWED for, which is a
# different claim and is answered from the record rather than from an opinion:
# a model asked "is there work left" answers yes cheaply, and three boundaries
# reporting the same next action is the work not moving whatever it says.
#
# Read from the checkpoints the conversation has already written, because that
# is the only place the answer exists: the boundary being decided has not
# reflected yet.


@dataclass(frozen=True)
class BoundaryBounds:
    max_consecutive_continues: int
    repeat_limit: int


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
        max_consecutive_continues=int(
            _require(section, "max_consecutive_continues", "roles.yaml boundary")
        ),
        repeat_limit=int(_require(section, "repeat_limit", "roles.yaml boundary")),
    )


def why_not_continue(chat_id: str) -> str:
    """Why this conversation may not answer `continue` again, or `""`.

    Three refusals, all read off the record and none of them an opinion about
    the work:

    1. **Nothing was owed last time.** The previous boundary answered
       `continue` and reported neither a next action nor anything remaining.
       Continuing again after that is continuing because continuing is
       possible.
    2. **The same next action, repeatedly.** The work is not moving, whatever
       the answer says it is doing.
    3. **A budget the agent cannot argue with.** Consecutive continues on one
       conversation are bounded, and the bound is config.

    A refusal is a sentence, because it is written onto the record and read by
    a person. Empty means the continue stands.

    Fails OPEN. A record that cannot be read is not evidence that a loop is
    running, and refusing on it would end a conversation for a disk error.
    """
    try:
        bounds = load_boundary_bounds()
        recent = checkpoints.recent_for_chat(
            chat_id, limit=max(bounds.max_consecutive_continues, bounds.repeat_limit)
        )
    except Exception:  # noqa: BLE001
        log.exception("continuity: could not read the boundaries %s has crossed", chat_id)
        return ""
    if not recent:
        return ""

    run = 0
    for checkpoint in recent:
        if checkpoint.outcome != "continue":
            break
        run += 1
    if run == 0:
        return ""

    previous = recent[0]
    if not previous.next_action and not previous.remaining:
        return (
            "the boundary before this one carried the work on and reported "
            "nothing left to do and nothing to do next, so there is nothing "
            "for this one to carry"
        )

    repeats = 0
    for checkpoint in recent[:run]:
        if checkpoint.next_action != previous.next_action:
            break
        repeats += 1
    if previous.next_action and repeats >= bounds.repeat_limit:
        return (
            f"the last {repeats} boundaries all reported the same next action, "
            f"{previous.next_action!r}, so the work is not moving"
        )

    if run >= bounds.max_consecutive_continues:
        return (
            f"this conversation has carried the work on {run} times in a row, "
            f"which is the bound in roles.yaml"
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
