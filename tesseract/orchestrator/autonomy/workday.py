"""Which steps the day may still work, and what one wake is asked.

The morning proposes and stops. This is the other half: once an hour inside the
operator's day, the same conversation is asked to take one step forward and
close it if the evidence is there. Nothing here proposes anything, and nothing
here decides a step is done: `task_close` and the project's own checks do that.

**A step is an open task on a project the day may work, with room left today.**
Both halves are `morning.py`'s and are asked of it rather than re-answered, so
a step can never be worked on a project a step could not have been proposed
for. `turns/tasks.py::TAKEABLE` is the third half and is also borrowed: it is
the runtime's existing list of states a conversation may take a task up from,
so `blocked` and `awaiting_operator` are left where they are, waiting on the
operator, exactly as they are in the chair.

**`running` counts as open, and it has to.** A turn that ends without closing
its task leaves it `running` (`note_turn_ended` moves it only on a recovery
park), so a step worked across three wakes is `running` for two of them.
Excluding it would mean the day worked its first step once and then reported
nothing to do for the rest of the day.

**Two conversations can reach one task and nothing here stops that.** If the
operator takes a task up in the cockpit at eleven and a wake takes the same one
at noon, both are `take_up` on a `running` item and both are allowed. That is
the runtime's behaviour today for any two conversations rather than something
this row introduces, it has not been measured, and a lock invented here would
be a guess about a race nobody has seen. What is new is that one of the two
conversations is nobody, which is why it is written down instead of left to be
discovered.

**The prompt carries what the record says and nothing else**, the same rule the
morning's prompt holds: every block names the file it was read from, and the
only authored text is the ask.

**One step per wake, and the day rotates between projects rather than draining
one.** Both halves are the operator's ruling of 2026-09-09: series, because a
second step at once would need a second session and a second budget read; and
rotation, because oldest-first across everything let one project's backlog take
every wake for days while the others sat still.
"""

from __future__ import annotations

import logging

from tesseract.orchestrator.autonomy.models import AgendaItem, AgendaSource
from tesseract.orchestrator.autonomy.morning import room_left, workable_projects
from tesseract.orchestrator.projects.models import Project

log = logging.getLogger(__name__)

#: How much of a step's own words reach one wake's prompt. A goal and its
#: criteria are capped at 500 and 2000 characters where they are written, and
#: the criteria is the half a wake has to read whole, so only the evidence of
#: earlier attempts is trimmed here.
_REASON_CHARS = 300


class Step:
    """One open step: the task, the project it is on, and the room it has left.

    A plain object rather than a tuple because three of these are read by name
    at every call site, and `step[2]` for "what it may still spend" is the
    shape that gets read as the wrong element a month later.
    """

    __slots__ = ("item", "project", "room")

    def __init__(self, item: AgendaItem, project: Project, room: float) -> None:
        self.item = item
        self.project = project
        self.room = room


def open_steps(
    items: list[AgendaItem],
    projects: list[Project],
    spent_today: dict[str, float] | None,
) -> list[Step]:
    """The steps a wake may work, one project at a time, oldest first within each.

    **Oldest first WITHIN a project, and a turn between projects.** A flat
    oldest-first list across everything starves: a project carrying the twenty
    oldest steps takes every wake for three days and the other four wait
    without anything being wrong. The day is still one step per wake, so the
    only question is which, and rotating is the answer that does not need a
    second session or a second budget read (operator ruling, 2026-09-09: keep
    series, fix the ordering).

    Projects enter the rotation ordered by their own oldest step, so the
    project that has waited longest still goes first; it just no longer goes
    seven times running.

    Pure over what it is handed, so the row does the reading and the tests do
    not have to build a disk. A `spent_today` of `None` yields no steps at all,
    through `room_left` rather than through a guard here: a ledger nobody could
    read leaves every step's ceiling invented. The row asks that question first
    and says so in its own words, because "nothing to work" and "I could not
    see what I had spent" are different afternoons.
    """
    from tesseract.orchestrator.turns.tasks import TAKEABLE

    workable = {project.id: project for project in workable_projects(projects)}
    by_project: dict[str, list[Step]] = {}
    for item in sorted(items, key=lambda i: i.created_at):
        if item.source is not AgendaSource.TASK or item.status not in TAKEABLE:
            continue
        project = workable.get(item.project_id)
        if project is None:
            continue
        room = room_left(project, spent_today)
        if room is None or room <= 0:
            continue
        by_project.setdefault(project.id, []).append(Step(item, project, room))
    return _rotate(by_project)


def _rotate(by_project: dict[str, list[Step]]) -> list[Step]:
    """Interleave per-project queues, longest-waiting project first.

    Deterministic, and it has to be: the wake reads this list and takes the
    first thing it can, so an order that shuffled between reads would make the
    day's own record impossible to follow. A project that runs out simply
    stops appearing; the rest close up rather than leaving a gap.
    """
    queues = sorted(by_project.values(), key=lambda q: q[0].item.created_at)
    out: list[Step] = []
    for round_index in range(max((len(q) for q in queues), default=0)):
        for queue in queues:
            if round_index < len(queue):
                out.append(queue[round_index])
    return out


def _last_attempt(item: AgendaItem) -> str:
    """What the previous wake left behind, as a sentence, or `""`.

    The one thing a wake cannot get from the conversation it rejoins: an
    attempt that ended when the app restarted wrote its outcome onto the task
    and left no turn to read. `Attempt.reason` is written by whoever ended the
    turn and is trimmed here rather than at the source, which keeps the record
    whole and the prompt bounded.
    """
    if not item.attempts:
        return ""
    last = item.attempts[-1]
    reason = (last.reason or "").strip()
    ended = f"the last attempt ended {last.outcome.value.replace('_', ' ')}"
    return f"{ended}: {reason[:_REASON_CHARS]}" if reason else ended


def step_lines(steps: list[Step]) -> tuple[str, ...]:
    """One paragraph per open step, read off the agenda records.

    `verify_snapshot` and not the project's live checks, because the snapshot
    is the contract as it stood when the step was accepted and is what
    `task_close` compares against. Showing the live commands here would tell a
    wake to satisfy a contract the close will not judge it by.
    """
    out: list[str] = []
    for step in steps:
        item = step.item
        checks = item.verify_snapshot.strip() or step.project.verify.as_contract()
        lines = [
            f"- {item.id} on {step.project.name} ({step.project.id})",
            f"  it is: {item.goal}",
            f"  it is done when: {item.success_criteria or 'nothing was declared'}",
            f"  status: {item.status.value.replace('_', ' ')}",
            f"  checks that decide it: {checks or 'none declared'}",
            f"  room left on this project today: ${step.room:.2f}",
            f"  root: {step.project.root}",
        ]
        was = _last_attempt(item)
        if was:
            lines.append(f"  {was}")
        out.append("\n".join(lines))
    return tuple(out)


def _ask() -> str:
    """The question one wake asks, and the two things it may not turn into.

    Written to the model and read by the operator. It names what the runtime
    already enforces rather than adding rules of its own: `task_work` refuses a
    task the operator holds, `task_close` refuses a close with no evidence, and
    `task_propose` refuses a step outside its project's budget. What is left
    here is the shape of the turn, which nothing else can say.
    """
    return "\n".join(
        (
            "Nobody asked for this turn. This is the same conversation as this "
            "morning, an hour or more later, and the steps below are what is "
            "still open on the projects you may work.",
            "",
            "Take ONE step up with `task_work` and carry it as far as it goes "
            "in this turn. If what it says it is done when has actually "
            "happened, close it with `task_close` and let its checks decide. "
            "If it is not done, stop where you are and say what the next turn "
            "should pick up. The record is what carries it, not this turn.",
            "",
            "Do not propose new work here. If a step turns out to need "
            "something outside its project, say so and leave it.",
            "",
            "If none of these can move right now, say NOTHING_TO_WORK and stop.",
        )
    )


def build(steps: list[Step]) -> str:
    """The whole wake prompt, or `""` when there is no step to work.

    Empty is a real answer and the row acts on it: a wake with nothing open
    calls no model at all, which is what makes an afternoon on a finished day
    cost file reads.
    """
    from tesseract.orchestrator.autonomy.morning_prompt import Block

    lines = step_lines(steps)
    if not lines:
        return ""
    return "\n\n".join(
        (
            _ask(),
            Block(
                title="The steps still open",
                source="agenda/active/",
                lines=lines,
            ).render(),
        )
    )


def read_open_steps(spent_today: dict[str, float] | None) -> list[Step]:
    """The two records `open_steps` is pure over, read from disk.

    Blocking on purpose and threaded by the caller, for `iter_active`'s reason:
    it walks a directory and parses one JSON file per open item. The spend is
    the caller's, because the row has to answer for an unreadable ledger before
    it can tell an empty afternoon from a blind one.
    """
    from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
    from tesseract.orchestrator.projects.store import ProjectStore

    return open_steps(
        list(AgendaStore().iter_active()),
        ProjectStore().list_projects(),
        spent_today,
    )


__all__ = ["Step", "build", "open_steps", "read_open_steps", "step_lines"]
