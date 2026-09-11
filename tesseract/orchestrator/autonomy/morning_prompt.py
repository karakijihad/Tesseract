"""What the morning asks, assembled from records rather than written as prose.

Nobody types this turn, so nothing about it is a person's judgement of what
today should be. Every fact in it is read off a file that already existed for
another reason, and every block says which file, because a model reading a
sentence with no source behind it has no way to tell what it may rely on and
will act on the sentence anyway. The thirteen `self_reflection` agenda items
this phase was written against came from exactly that: a prompt that asked what
might be worth doing and named nothing it could be judged against.

**The only text here that is not read from disk is `_ASK`.** It is the question
and its refusals, it is declared once, and `test_the_prompt_is_read_from_records`
holds the line by asserting that every other line of a built prompt is either a
block heading naming a real file or content found in that file.

**An empty block still renders, with one line saying it is empty.** The house
rule elsewhere is to drop a heading with nothing under it, because a model
answers a question it was asked. This is the one place that rule inverts: "no
task closed yesterday" and "the record was not read" are different mornings,
and a prompt that drops the block says neither. It is the same distinction
`morning.spent_today_by_project` keeps between `None` and `{}`, one layer up.

**The three blocks that grow without limit are capped, at the source of each.**
A project's progress log grows for the life of the project, the diary grows for
the life of the machine, and the agenda history grows per closed task, so
`_PROGRESS_CHARS`, `_DIARY_CHARS` and `_CLOSED_ROWS` are what one morning may
carry of them. The prompt's size is therefore set by how many projects are
priced rather than by how long the machine has run.

The other two are deliberately uncapped, because a cap would have to cut
something a person chose. `carried.txt` is the operator's own short list, and
SOUL.md's open questions are approved one at a time, so both grow by a decision
and not by accretion. If either ever stops being true it is this comment that
was wrong, not the block.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import yaml

from tesseract.kernel.tools.untrusted_envelope import wrap
from tesseract.orchestrator.projects.models import Project, budget_line
from tesseract.paths import TESSERACT_DIR

log = logging.getLogger(__name__)

#: How much of one project's last progress note reaches the prompt. Enough to
#: say where the project was left, not enough for a long note to crowd out the
#: eleven other projects beside it.
_PROGRESS_CHARS = 700

#: How much of the last diary entry reaches it. `diary_append` caps an entry
#: well below this, so this bounds a hand-written one rather than a normal one.
_DIARY_CHARS = 600

#: How many closed tasks are worth reading as "what happened". A day that
#: closed more than this had a person driving it, and the morning does not need
#: every row to see the shape of it.
_CLOSED_ROWS = 12

_EMPTY = "nothing recorded"


@dataclass(frozen=True)
class Block:
    """One section: what it is, the file it was read from, and the lines.

    `source` is a repo-relative or home-relative path written the way the
    operator would type it, because they read this over the model's shoulder
    and a resolved absolute path tells them where the machine is rather than
    where the fact is.
    """

    title: str
    source: str
    lines: tuple[str, ...]

    def render(self) -> str:
        body = self.lines or (_EMPTY,)
        return "\n".join([f"## {self.title} (from {self.source})", *body])


def max_steps() -> int:
    """How many steps one morning may propose, from `agenda.yaml::morning`."""
    from tesseract.orchestrator.autonomy.morning import morning_setting

    return morning_setting("max_steps")


def _ask(steps: int, has_ideas: bool = False) -> str:
    """The question, and the four things a step may not be.

    Written to the model and read by the operator, so it says what is decided
    here rather than naming the functions that decide it.

    **Every refusal below is enforced, and this text is what tells the model
    why before it spends a call finding out.** `task_propose::
    _why_not_unattended` asks `morning.why_a_step_is_refused` on any session of
    kind `autonomy`, which answers for place, for the project's room today, for
    a missing estimate and for criteria naming a check the project does not
    declare. This paragraph claimed the opposite in each direction once, and
    both were worse than saying nothing: a reader who trusts either stops
    looking. It is checked against `why_a_step_is_refused` when that function
    changes.
    """
    return "\n".join(
        (
            "Nobody asked for this turn. The app is open, the projects below "
            "are yours, and everything under them is read from the record.",
            "",
            f"Decide what is worth doing today. Propose zero to {steps} steps "
            "with `task_propose`, and stop.",
            "",
            "Read what is already open first. A day works ONE step per wake, "
            "so a backlog longer than the hours left is a day that is already "
            "full: propose nothing unless what you have in mind matters more "
            "than what is waiting, and say which it would displace.",
            "",
            "A step names one project id from the list below. It says what "
            "will be true when it is done in terms of a check that project "
            "declares, and it estimates what it will cost. A step that would "
            "cost more than the room left on its project is not one you may "
            "propose, and neither is one for a project that is not listed.",
            "",
            *_starting_an_idea(has_ideas),
            "If nothing is worth doing, reply NO_STEPS and stop. A morning "
            "with no step is an ordinary morning.",
            "",
            "Do not begin the work in this turn.",
        )
    )


def _starting_an_idea(has_ideas: bool) -> tuple[str, ...]:
    """The two lines about accepted ideas, and nothing when there are none.

    **Carried only when there is one to start.** It was unconditional for one
    commit, which put an instruction in every morning that most mornings could
    not act on, and on a shipped `max` install could never act on at all:
    `project_new` is `ask` there and a headless session has no approver, so
    `decide.py` denies it before the tool's own branching is reached. Telling a
    turn to make a call that will be refused spends a model call to discover
    it. Making that refusal answerable from a phone is AR-29 item 7, on AR-28
    item 8's advice card; until then the honest thing is to ask only when
    there is something to ask about, and to say what a refusal means.
    """
    if not has_ideas:
        return ()
    return (
        "One idea below has been accepted and nothing has started it. Start "
        "it instead of proposing a step: call `project_new` WITHOUT "
        "`confirmed`, which puts the plan to the operator and creates "
        "nothing. Ask where it should live, whether it is a git repository, "
        "what it may spend a day and what checks decide its work. If that "
        "call is refused because nobody is here to approve it, say so and "
        "stop; the idea keeps until they are.",
        "",
    )


def listed(
    projects: list[Project], room: dict[str, float | None]
) -> tuple[tuple[Project, float], ...]:
    """The projects the prompt names, with what each may still spend today.

    **Two blocks describe these and they have to be the same list.** The facts
    and the progress notes are rendered separately, because only one of them
    came out of somebody's working tree, and a reader moving between the two
    headings is looking at one set of projects. Deciding that twice is how the
    two come to disagree, and a model reading a list that contradicts itself
    has no way to tell which half is right.

    **Zero room is the same answer as no room**, and listing it would be worse
    than useless: every step costs something, so no step could fit, and the
    only thing naming it achieves is a billed turn spent explaining that. It is
    reached two ways that mean the same thing on the day, by a budget the
    operator set to zero and by one already spent, and neither is an error.
    """
    out: list[tuple[Project, float]] = []
    for project in projects:
        left = room.get(project.id)
        if left is None or left <= 0:
            continue
        out.append((project, left))
    return tuple(out)


def project_lines(
    projects: list[Project], room: dict[str, float | None]
) -> tuple[str, ...]:
    """One paragraph per project: what it is, what it may spend, where it was left.

    `room` is keyed by project id and is the caller's, because working out
    what is left today needs the ledger and this module reads files that sit
    still. A project whose room could not be worked out is not listed at all
    rather than listed with an unknown ceiling.

    Which projects are named is `listed`'s answer, shared with the block that
    says where each was left.
    """
    out: list[str] = []
    for project, left in listed(projects, room):
        checks = "; ".join(project.verify.as_contract().splitlines()) or "none declared"
        out.append(
            "\n".join(
                (
                    f"- {project.name} ({project.id})",
                    f"  budget: {budget_line(project.budget_usd)}, "
                    f"${left:.2f} left today",
                    f"  root: {project.root}",
                    f"  checks: {checks}",
                )
            )
        )
    return tuple(out)


def progress_lines(projects: list[Project], room: dict[str, float | None]) -> tuple[str, ...]:
    """Where each project was left, read out of its own working tree.

    **Lifted out of the project bullet on purpose.** Every other line in this
    prompt is written by the runtime or by the operator; this one is the
    contents of a file inside a directory other people push to. It is marked
    as data by `build`, and it cannot be marked while it sits inside a bullet
    of facts that are not.

    Same projects and the same order as `project_lines`, because both walk
    `listed`.
    """
    out: list[str] = []
    for project, _left in listed(projects, room):
        entry = last_progress_entry(Path(project.root))
        out.append(f"- {project.name}: {entry or _EMPTY}")
    return tuple(out)


def last_progress_entry(root: Path) -> str:
    """The newest dated section of `<root>/PROGRESS.md`, trimmed, or `""`.

    Dated, not last. `snake/PROGRESS.md` ends with a `## Deployment` section
    that has stood since August, and a reader taking the final heading would
    report a standing note as the last thing that happened. A section whose
    heading does not parse as a date is not an entry.

    The whole file is read because a progress log is measured in kilobytes and
    the alternative is seeking backwards through a growing file for a heading
    that may not be there.
    """
    path = root / "PROGRESS.md"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # `UnicodeDecodeError` is a `ValueError`, not an `OSError`, and every
        # file this module reads is written by a person or by an outside tool.
        # One PROGRESS.md saved in the machine's ANSI codepage would otherwise
        # raise all the way out of `build`, and the row would record the whole
        # morning as failed over a file the other fourteen projects have
        # nothing to do with.
        return ""
    heading = ""
    body: list[str] = []
    found: tuple[str, list[str]] | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            if heading:
                found = (heading, body)
            candidate = line[3:].strip()
            heading = candidate if _is_a_date(candidate) else ""
            body = []
        elif heading:
            body.append(line)
    if heading:
        found = (heading, body)
    if found is None:
        return ""
    said = " ".join(part.strip() for part in found[1] if part.strip())
    return f"{found[0]}: {said[:_PROGRESS_CHARS]}".rstrip()


def _is_a_date(heading: str) -> bool:
    """Whether this heading opens a dated entry, judged on its LEADING token.

    Not the whole string, and the difference is a live file rather than a
    hypothetical. `document-skills-progress` writes a second note on a day it
    has already written one and heads it `## 2026-09-03, second pass`, which
    `three-layers-improvement/PROGRESS.md` carries today. A whole-string check
    reads that as prose, drops the entry, and answers with the note ABOVE it,
    so the morning is shown a status one entry out of date with nothing saying
    so. A reader that fails loudly would have been better than that; a reader
    that reads the heading is better than both.

    The heading is kept whole for the line it labels, because "second pass" is
    part of what the entry is.
    """
    leading = heading.split(",", 1)[0].split()
    try:
        date.fromisoformat(leading[0] if leading else "")
    except ValueError:
        return False
    return True


def accepted_idea_lines(events: list[Any]) -> tuple[str, ...]:
    """The ideas the operator has accepted and nothing has started yet.

    **This is the whole handoff, and it is a record rather than a trigger.**
    Approving a `project_proposal` creates nothing (operator ruling,
    2026-09-09): it says the idea is worth starting, and where it lives, what
    it may spend and what checks decide its work are `project_new`'s interview.
    Nothing pings a turn when a card is approved, so the next morning reads the
    card the way it reads every other record, and asks.

    An idea whose project now exists is not listed. The registry is the
    authority on that, not the card, because the operator can start one by
    hand in their own chat and the card would go on asking for it forever.

    **Matched on the NAME, which is the weaker half of this.** `is_the_mornings`
    refuses to key on a name for exactly the reason that applies here, that a
    name is the operator's to change, and there is nothing better to key on:
    approving a card creates nothing, so no id is ever minted for it to carry.
    What a rename costs is one idea reappearing on one morning and being
    declined once, which is a cost the operator can see and end. Inventing a
    field on the project to hold the card it came from would be a mechanism
    for a case nobody has hit.
    """
    from tesseract.orchestrator.projects.store import ProjectStore

    try:
        names = {p.name.strip().casefold() for p in ProjectStore().list_projects()}
    except Exception:  # noqa: BLE001 - a registry that will not read lists them all
        # Listing an idea already started costs one sentence in a prompt. Not
        # listing one that was never started loses it, and nothing else asks.
        names = set()
    out: list[str] = []
    for event in events:
        payload = getattr(event, "payload", None) or {}
        name = str(payload.get("name") or "").strip()
        if not name or name.casefold() in names:
            continue
        out.append(
            "\n".join(
                (
                    f"- {name}",
                    f"  it would be: {payload.get('goal') or _EMPTY}",
                    f"  why: {payload.get('why_now') or _EMPTY}",
                    f"  first step: {payload.get('first_step') or _EMPTY}",
                    f"  accepted on {str(getattr(event, 'ts', ''))[:10]}",
                )
            )
        )
    return tuple(out)


def open_step_lines(steps: list[Any]) -> tuple[str, ...]:
    """What is already owed, summarised: how many, on what, and the oldest.

    **The morning could not see this and it decided anyway.** It proposes at
    09:00 and only the workday at 10:00 ever read the open steps, so a machine
    with twenty-nine waiting looked exactly like a machine with none, and the
    morning would add a thirtieth. A day works one step per wake, so a backlog
    is the single most useful thing to know before proposing more.

    A SUMMARY rather than the list. The morning does not work these and does
    not need their criteria or their checks; it needs to know the day is full.
    The workday's own prompt carries them whole, from the same records.
    """
    if not steps:
        return ()
    per_project: dict[str, int] = {}
    for step in steps:
        per_project[step.project.name] = per_project.get(step.project.name, 0) + 1
    where = ", ".join(
        f"{name} ({count})" for name, count in sorted(per_project.items())
    )
    oldest = steps[0].item
    return (
        f"- {len(steps)} already open, on {where}",
        f"- the oldest is {oldest.created_at.date().isoformat()}: {oldest.goal}",
    )


def open_steps_today() -> list[Any]:
    """The steps a wake would work right now, or an empty list.

    Asks `workday.open_steps` rather than re-deriving it, so the morning and
    the wakes after it cannot disagree about what is waiting. A ledger that
    will not answer yields nothing, which reads as an empty block: the morning
    is not the place that refuses over an unreadable ledger, the row is, and it
    has already done so by the time this is called.
    """
    from tesseract.orchestrator.autonomy.morning import spent_today_by_project
    from tesseract.orchestrator.autonomy.workday import read_open_steps

    try:
        return read_open_steps(spent_today_by_project())
    except Exception:  # noqa: BLE001 - an unreadable agenda is an empty block
        log.warning("morning: what is already open could not be read", exc_info=True)
        return []


def accepted_ideas() -> list[Any]:
    """The approved `project_proposal` cards, newest first, or an empty list."""
    from tesseract.kernel.tools.project_propose import CARD_KIND
    from tesseract.paths import home_logs_root
    from tesseract.workspace_events import EventStore

    try:
        return EventStore(home_logs_root()).list_events(
            kinds=(CARD_KIND,), status="approved", limit=20
        )
    except Exception:  # noqa: BLE001 - a queue that will not read is an empty block
        log.warning("morning: the accepted ideas could not be read", exc_info=True)
        return []


def closed_lines(rows: list[dict[str, Any]]) -> tuple[str, ...]:
    """What finished, with who said so and which project it was on.

    `verification_by` is carried because it is the difference between a task a
    check closed and one a model asserted was done, and a morning reading
    yesterday to decide today needs that difference more than it needs the
    duration.
    """
    out: list[str] = []
    for row in rows[-_CLOSED_ROWS:]:
        by = str(row.get("verification_by") or "").strip() or "nobody"
        project = str(row.get("project_id") or "").strip() or "no project"
        out.append(
            f"- {row.get('status')}: {row.get('goal')} "
            f"({project}, verified by {by})"
        )
    return tuple(out)


def soul_questions(text: str) -> tuple[str, ...]:
    """The bullets under SOUL.md's `## Open questions`, as written.

    That section is the one place the assistant records what it does not know,
    which is the nearest thing on this machine to an idea it has had. It is
    read and never acted on directly: an open question is not a project, and
    item 6 of the phase is what turns one into a card.
    """
    return tuple(_section_bullets(text, "Open questions"))


def _section_bullets(text: str, heading: str) -> list[str]:
    """The bullets under one `##` heading, each rejoined onto one line.

    Rejoined because SOUL.md is a hand-written document and its bullets wrap:
    taking only the lines that start with `- ` cut the live open question in
    half at "one lesson I have not", which reads as a complete sentence and is
    not one. A wrapped continuation is an indented line under an open bullet.
    """
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip() == heading
            continue
        if not inside:
            continue
        if line.strip().startswith("- "):
            out.append(line.strip())
        elif out and line.strip() and line.startswith((" ", "\t")):
            # Only an INDENTED line folds. The prose SOUL.md carries between
            # the heading and its bullets sits at column 0, so it is ignored
            # rather than folded onto a bullet it does not belong to.
            out[-1] = f"{out[-1]} {line.strip()}"
    return out


def last_diary_entry(text: str) -> str:
    """The last `**HH:MM**` entry in a day's diary file, trimmed, or `""`.

    One line per entry is `diary_append`'s own format, and the leading `**` is
    what separates an entry from the `# Diary` header the first write of the
    day puts above them.
    """
    lines = [line.strip() for line in text.splitlines() if line.startswith("**")]
    return lines[-1][:_DIARY_CHARS] if lines else ""


def _read(path: Path) -> str:
    """One record as text, or `""`. Unreadable and absent answer alike here.

    `UnicodeDecodeError` for `last_progress_entry`'s reason: it is a
    `ValueError`, so catching `OSError` alone lets a badly encoded SOUL.md or
    diary file take down a morning that has nothing to do with it.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _newest_diary_file(diary_dir: Path) -> Path | None:
    try:
        files = sorted(diary_dir.glob("*.md"))
    except OSError:
        return None
    return files[-1] if files else None


def since_yesterday() -> datetime:
    """Local midnight at the start of yesterday, as an aware datetime.

    The window the morning reads, and it is two days wide rather than one on
    purpose: a morning at eight needs what closed after the last one, and
    yesterday afternoon's work is that. Aware because `closed_since` compares
    against parsed `closed_at` values, which are UTC.
    """
    from tesseract.lib.clock import yesterday

    return datetime.combine(yesterday(), time.min).astimezone()


def blocks(ideas: list[Any] | None = None) -> tuple[Block, ...]:
    """Everything the morning reads except the projects, which need the ledger.

    Each entry is one file and one question of it. Failures are empty blocks
    rather than exceptions: a machine with no diary yet is a new machine, not a
    broken one, and a morning that refused to run because a file was absent
    would be the least useful failure this runtime could have.
    """
    from tesseract.brain.playbook_set import carried_path, load_carried_names
    from tesseract.orchestrator.autonomy.agenda_history import closed_since
    from tesseract.paths import home_dir, workspace_dir

    diary_dir = home_dir() / "memory-store" / "diary"
    newest = _newest_diary_file(diary_dir)
    entry = last_diary_entry(_read(newest)) if newest is not None else ""
    carried = sorted(load_carried_names())
    return (
        Block(
            title="What closed since yesterday",
            source="agenda/history/",
            lines=closed_lines(
                closed_since(since_yesterday(), statuses={"done", "failed"})
            ),
        ),
        Block(
            title="What you are still unsure of",
            source="workspace/SOUL.md",
            lines=soul_questions(_read(workspace_dir() / "SOUL.md")),
        ),
        Block(
            title="Your last diary entry",
            source=(
                f"memory-store/diary/{newest.name}" if newest is not None
                else "memory-store/diary/"
            ),
            lines=(entry,) if entry else (),
        ),
        Block(
            title="The playbooks you are carrying",
            source=f"workspace/skills/{carried_path().name}",
            lines=tuple(f"- {name}" for name in carried),
        ),
        Block(
            title="The steps already open",
            source="agenda/active/",
            lines=open_step_lines(open_steps_today()),
        ),
        Block(
            title="Ideas the operator has accepted and nobody has started",
            source="logs/workspace/events.jsonl",
            lines=accepted_idea_lines(
                accepted_ideas() if ideas is None else ideas
            ),
        ),
    )


def build(projects: list[Project], room: dict[str, float | None]) -> str:
    """The whole prompt, or `""` when there is neither a project nor an idea.

    Empty is a real answer and the caller acts on it: a morning with nothing to
    work and nothing to start does not call a model at all, which is what makes
    an ordinary morning cost nothing.

    **Two reasons to open the day, not one.** A project with room is the
    obvious one. An idea the operator has accepted is the other, and it does
    not need a funded project to sit beside: starting it is `project_new`'s
    interview, which spends nothing and asks them where it lives.
    """
    # Read BEFORE the early return, because an accepted idea is a reason to
    # open the day on its own. The operator most likely to have accepted one is
    # the operator with nothing priced yet, and returning here on an empty
    # project list made the handoff unreachable in exactly that case.
    ideas = accepted_ideas()
    lines = project_lines(projects, room)
    if not lines and not accepted_idea_lines(ideas):
        return ""
    sections = [
        _ask(max_steps(), bool(accepted_idea_lines(ideas))),
        Block(
            title="The projects you may work today",
            source="projects/registry.json",
            lines=lines,
        ).render(),
        # **The one block that came out of somebody's working tree**, and the
        # only one wrapped. `PROGRESS.md` sits inside a directory a
        # collaborator or a CI job can write, so a note in it reaches a turn
        # nobody is watching, in a mode where most tools resolve to auto. The
        # envelope is the boundary `file_read` already puts around a file's
        # contents; this path assembles the text itself and so has to put it
        # there itself. Everything else here is written by the runtime or by
        # the operator and is not marked, because marking everything would
        # mark nothing.
        wrap(
            tool="morning",
            output=Block(
                title="Where each project was left",
                source="<project root>/PROGRESS.md",
                lines=progress_lines(projects, room),
            ).render(),
            source="PROGRESS.md",
        ),
        *(block.render() for block in blocks(ideas)),
    ]
    return "\n\n".join(sections)


__all__ = [
    "Block",
    "build",
    "blocks",
    "accepted_idea_lines",
    "accepted_ideas",
    "closed_lines",
    "last_diary_entry",
    "last_progress_entry",
    "listed",
    "max_steps",
    "open_step_lines",
    "open_steps_today",
    "progress_lines",
    "project_lines",
    "since_yesterday",
    "soul_questions",
]
