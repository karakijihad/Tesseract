"""What changed since the operator last looked, read off the records.

Growth the operator cannot read is indistinguishable from bloat. The runtime
already keeps every piece of the answer, and keeps them in seven places with
seven readers, each one a day or a panel wide: what shipped and the check that
proved it, what failed and why, what is waiting on them, what it learned and
the words it added to its own soul, what it spent, what is still broken. None
of them joins on *since you were last here*, which is the only question this
file asks.

**Renderer only, and there is no model in it.** Structure before model: a
narrator over an unjudged record writes fluently about a failure that had
already been fixed. Every line here is a record plus the id or path it came
from, so a reader can go and look, and a line nobody can trace is a story.

**One reader, every surface.** The brief carries it the morning after an
absence, the tool answers it wherever the question is typed, and the panel
shows the same text. A per-channel digest would be a second answer to a
question the operator asks the same way from every chair.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tesseract.lib.clock import to_local

log = logging.getLogger(__name__)

#: How long the operator has to be gone before the brief carries the note by
#: itself. Not a preference: it is what "away" means here, and a shorter
#: window would put a return note in front of somebody who never left.
ABSENT_AFTER = timedelta(days=1)

#: A section that would run longer than this is a list nobody reads. The
#: oldest are kept for *Waiting on you*, where waiting longest is the point,
#: and the newest everywhere else.
_PER_SECTION = 12


@dataclass(frozen=True)
class Line:
    """One fact and the record it came from."""

    text: str
    record: str

    def rendered(self) -> str:
        return f"- {self.text} [{self.record}]"


@dataclass(frozen=True)
class Section:
    title: str
    lines: tuple[Line, ...]


def is_owed(since: datetime | None, *, now: datetime | None = None) -> bool:
    """Whether an absence beginning at `since` is long enough to be owed a note.

    Takes the marker rather than reading it, so a caller that already has one
    can answer both halves of the question from a single read. The route asked
    for `since` and then let `owed` read the marker again, which let one
    response carry a `since` describing the old absence beside an `owed`
    describing a newer one.

    `None` is a machine that has never seen the operator: nothing happened, so
    there is nothing to say, and a fresh install must not greet its owner with
    a report on an absence it never observed.
    """
    if since is None:
        return False
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return moment - since > ABSENT_AFTER


def owed(*, now: datetime | None = None) -> datetime | None:
    """The marker when the operator has been away long enough to be owed a
    note, otherwise None. One read, for a caller that has none of its own."""
    from tesseract.lib import last_seen

    mark = last_seen.read()
    return mark if is_owed(mark, now=now) else None


def render(
    *,
    since: datetime | None,
    now: datetime | None = None,
    event_store: Any | None = None,
    ledger: Any | None = None,
) -> str:
    """The note, as the text every surface shows.

    `since` is the last-seen marker. `None` says this machine has no record of
    when the operator was last here, which is an answer and not an error.

    `event_store` and `ledger` are handed in the way the brief renderer is
    handed its digester and its memory store: they are live objects the
    backend already holds, and resolving a second one here would be a second
    answer to where the inbox and the ledger are. Everything else resolves
    from `TESSERACT_HOME` at call time.
    """
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if since is None:
        return (
            "This machine has no record of when you were last here, so there "
            "is nothing to measure since against."
        )

    sections = [
        _section("Shipped", lambda: _shipped(since)),
        _section("Tried and could not", lambda: _failed(since)),
        _section("Waiting on you", lambda: _waiting(event_store, now=moment)),
        _section("Learned", lambda: _learned(since, event_store)),
        _section("Spent", lambda: _spent(since, ledger)),
        _section("Health", lambda: _health(now=moment)),
    ]

    head = f"You were last here on {_when(since)}, {_how_long(moment - since)} ago."
    filled = [s for s in sections if s.lines]
    if not filled:
        return f"{head} Nothing to report."

    out = [head, ""]
    for section in filled:
        out.append(f"{section.title}:")
        out.extend(line.rendered() for line in section.lines)
        out.append("")
    return "\n".join(out).strip()


def _section(title: str, build: Callable[[], list[Line]]) -> Section:
    """One section, and one store's bad day costs only that section.

    Six readers over six stores, and each of them can be mid-rotation, mid-
    retention-sweep or held open by another process. A single guard around the
    whole note would turn any one of those into no note at all, and no guard
    at all would turn it into a 500 on the panel and a broken brief. This is
    the runtime's own rule about a break taking one capability off the board
    rather than stopping everything.

    Loud in the log and quiet on the page: a section that failed and a section
    with nothing in it look the same to a reader, so the log is the only place
    the difference can live.
    """
    try:
        return Section(title, tuple(build()))
    except Exception:  # noqa: BLE001
        log.exception("return note: %s could not be read", title)
        return Section(title, ())


# ── the sections ───────────────────────────────────────────────────────


def _shipped(since: datetime) -> list[Line]:
    """Tasks that closed `done`, with what decided they were done.

    `verification_by` is on the history row, so a task whose full record has
    aged out under retention still says who wrote the evidence. The evidence
    itself and the project's live URL need the record and the registry, and
    both are missing rather than wrong when they are gone.
    """
    from tesseract.orchestrator.autonomy import agenda_history

    store = _store()
    lines: list[Line] = []
    for row in agenda_history.closed_since(since, statuses={"done"})[-_PER_SECTION:]:
        parts = [str(row.get("goal") or "a task with no goal on its row")]
        project = _project_of(str(row.get("project_id") or ""))
        if project is not None:
            parts.append(f"On {project.name}")
            # `live` is a verify command, not a label: the gate fetches it and
            # expects a 2xx, so a URL here is one the check actually reached.
            if project.verify.live:
                parts.append(f"Live at {project.verify.live}")
        parts.append(
            _verdict(
                str(row.get("verification_by") or ""),
                _verification_text(str(row.get("id") or ""), store),
            )
        )
        lines.append(Line(". ".join(parts), str(row.get("id") or "no id")))
    return lines


def _failed(since: datetime) -> list[Line]:
    """Tasks that closed `failed`, with the reason the close had to carry.

    A failure cannot be recorded without one: `transition_to` refuses. The
    reason lives on the last transition of the full record, so a task whose
    record has aged out says only that it failed, which is still worth the
    line.
    """
    from tesseract.orchestrator.autonomy import agenda_history

    store = _store()
    lines: list[Line] = []
    for row in agenda_history.closed_since(since, statuses={"failed"})[-_PER_SECTION:]:
        goal = str(row.get("goal") or "a task with no goal on its row")
        reason = _failure_reason(str(row.get("id") or ""), store)
        text = f"{goal}: {reason}" if reason else f"{goal}. No reason on the record."
        lines.append(Line(text, str(row.get("id") or "no id")))
    return lines


def _waiting(event_store: Any | None, *, now: datetime) -> list[Line]:
    """Everything in the inbox that asks the operator a question, oldest first.

    `DECIDABLE_KINDS` is the list, and it is read rather than restated: two
    answers to what is waiting is how a surface starts under-reporting.
    """
    if event_store is None:
        return []
    from tesseract.workspace_events.events import DECIDABLE_KINDS

    try:
        events = event_store.list_events(kinds=DECIDABLE_KINDS, status="pending")
    except Exception:
        log.warning("return note: could not read the inbox", exc_info=True)
        return []

    lines: list[Line] = []
    for ev in sorted(events, key=lambda e: e.ts)[:_PER_SECTION]:
        waited = _parse(ev.ts)
        age = f", waiting {_how_long(now - waited)}" if waited is not None else ""
        lines.append(Line(f"{ev.title} ({ev.kind}){age}", ev.event_id))
    return lines


def _learned(since: datetime, event_store: Any | None) -> list[Line]:
    """The soul bullets that landed, and the playbooks that changed.

    The bullets come from the diff rather than from the approvals, because a
    card says a change was approved and the diff says what the soul now says.
    The snapshot the diff runs against is the one the edit itself kept, so the
    record each line names is the file a reader can open.
    """
    return _soul_bullets(since) + _playbooks(since, event_store)


def _spent(since: datetime, ledger: Any | None) -> list[Line]:
    """What it cost, in total and by project, against the cap when known.

    A row names a task, never a project, so the split joins through the same
    history rows the two task sections read. Work with no project is its own
    line rather than being dropped into one of theirs.
    """
    from tesseract.brain.cost import spent as spent_rows
    from tesseract.brain.cost.ledger import configured_log_path

    path = getattr(ledger, "log_path", None) or configured_log_path()
    if path is None or not Path(path).is_file():
        return []
    try:
        rows = spent_rows.attributed(
            spent_rows.parse(Path(path).read_text(encoding="utf-8").splitlines())
        )
    except OSError:
        log.warning("return note: could not read the ledger", exc_info=True)
        return []

    recent = [r for r in rows if r.ts.astimezone(timezone.utc) > since]
    total = sum(r.cost_usd for r in recent)
    if not recent or total <= 0:
        return []

    record = _relative(Path(path))
    cap = getattr(ledger, "cap_usd", None)
    head = f"{_money(total)} over {len(recent)} calls"
    # Three states, not two. No ledger reached means the cap is unknown; a cap
    # of zero means nothing is capped, which is a real answer and a different
    # one. Collapsing them made "the reader could not see the ceiling" and
    # "there is no ceiling" the same sentence.
    if cap is None:
        head += ", and the daily cap could not be read"
    elif not cap:
        head += ", against no daily cap"
    else:
        head += f", against a daily cap of {_money(float(cap))}"
    lines = [Line(head, record)]

    by_project: dict[str, float] = {}
    projects = _projects_by_task()
    for row in recent:
        key = projects.get(row.task_id, "")
        by_project[key] = by_project.get(key, 0.0) + row.cost_usd
    if set(by_project) == {""}:
        # One bucket and it is the unattributed one, which the total already
        # said. A split has to split something.
        return lines
    for key, amount in sorted(by_project.items(), key=lambda kv: -kv[1])[:_PER_SECTION]:
        project = _project_of(key)
        name = project.name if project is not None else "work with no project"
        lines.append(Line(f"{name}: {_money(amount)}", key or record))
    return lines


def _health(*, now: datetime) -> list[Line]:
    """Faults the watchman is still seeing.

    Only what is still standing. A fault that recovered is dropped from the
    store two days later, so an absence longer than that cannot be told what
    recovered during it, and reporting the ones that happen to survive would
    make the list a function of how long the operator was gone.
    """
    from tesseract.orchestrator.watchman.judge import standing

    lines: list[Line] = []
    entries = [s for s in standing.load().values() if s.recovered_at is None]
    for fault in sorted(entries, key=lambda s: s.first_reported)[:_PER_SECTION]:
        since_first = _how_long(now - fault.first_reported.astimezone(timezone.utc))
        summary = fault.summary or fault.key
        lines.append(Line(f"{summary}. Standing for {since_first}", fault.key))
    return lines


# ── the readers behind them ────────────────────────────────────────────


def _soul_bullets(since: datetime) -> list[Line]:
    """Bullets the live soul has that it did not have when the operator left.

    Each snapshot holds what the soul said just BEFORE the edit that kept it,
    so the one to diff against is the earliest whose instant falls after
    `since`: nothing changed the soul between the operator leaving and that
    edit, which makes its content exactly what they last saw. Strictly after,
    because a snapshot taken while they were still here holds a state they
    watched change.

    That reading is only sound while the writer holds its side. The writer is
    `workspace_changes._keep_soul_predecessor`, and properties 2 and 3 of its
    invariant list are the two this depends on: a name that orders against an
    arbitrary instant, and content that is the state before the edit. Read
    that list before changing either end.

    No snapshot after `since` means no soul edit landed in the absence, and
    there is nothing to diff.
    """
    from tesseract.kernel.workspace_changes import soul_history_dir, soul_slot_instant
    from tesseract.paths import workspace_dir

    root = soul_history_dir()
    if not root.is_dir():
        return []
    kept = sorted(
        (instant, d)
        for d in root.iterdir()
        if d.is_dir() and (instant := soul_slot_instant(d.name)) is not None
        and instant > since
    )
    if not kept:
        return []
    snapshot = kept[0][1] / "SOUL.md"
    live = workspace_dir() / "SOUL.md"
    try:
        was = _bullets(snapshot.read_text(encoding="utf-8"))
        now_text = _bullets(live.read_text(encoding="utf-8"))
    except OSError:
        return []
    record = _relative(snapshot)
    return [Line(b.lstrip("- ").strip(), record) for b in now_text if b not in was][
        :_PER_SECTION
    ]


def _playbooks(since: datetime, event_store: Any | None) -> list[Line]:
    """Playbook approvals and revisions the operator settled, with the numbers
    the live revision has since measured."""
    if event_store is None:
        return []
    from tesseract.workspace_events.events import SETTLED

    try:
        events = event_store.list_events(kinds=("skill_approval", "skill_refinement"))
    except Exception:
        log.warning("return note: could not read the inbox", exc_info=True)
        return []

    lines: list[Line] = []
    for ev in sorted(events, key=lambda e: e.decided_at or e.ts):
        decided = _parse(ev.decided_at)
        if ev.status not in SETTLED or decided is None or decided <= since:
            continue
        verb = "added" if ev.kind == "skill_approval" else "revised"
        if ev.status in {"rejected", "deleted"}:
            verb = "declined"
        lines.append(Line(f"{ev.title}: {verb}{_numbers(ev)}", ev.event_id))
    return lines[-_PER_SECTION:]


def _numbers(ev: Any) -> str:
    """The live revision's reuse record, when the card names a playbook."""
    name = str((ev.payload or {}).get("skill_name") or (ev.payload or {}).get("name") or "")
    if not name:
        return ""
    from tesseract.brain import playbook_reuse

    try:
        window = playbook_reuse.measure(name, window_days=30)
    except Exception:
        return ""
    if not window:
        return ""
    live = sorted(window.items())[-1][1]
    if live.trouble is None:
        return f". Read {live.loads} times, nothing has graded it yet"
    return (
        f". Read {live.loads} times, {live.succeeded} worked and {live.failed} did not"
    )


def _verification_text(task_id: str, store: Any) -> str:
    item = _record(task_id, store)
    if item is None:
        return ""
    return " ".join(item.verification.split())[:200]


def _failure_reason(task_id: str, store: Any) -> str:
    item = _record(task_id, store)
    if item is None:
        return ""
    for transition in reversed(item.status_history):
        if transition.reason:
            return " ".join(transition.reason.split())[:200]
    return ""


def _store() -> Any:
    """One store per section, not one per row.

    Every task in this note is closed, so every `get` falls through to an
    archive scan. Building a store for each of them made the note's cost grow
    with the machine's whole history rather than with the length of the
    absence.
    """
    from tesseract.orchestrator.autonomy.agenda_store import AgendaStore

    return AgendaStore()


def _record(task_id: str, store: Any) -> Any | None:
    """The full agenda record, or None once retention has taken it."""
    if not task_id:
        return None
    try:
        return store.get(task_id)
    except Exception:
        log.debug("return note: could not read agenda record %s", task_id, exc_info=True)
        return None


def _projects_by_task() -> dict[str, str]:
    """Which project each task belongs to, closed and still running alike.

    The history rows answer this for finished work, and answering from them
    ALONE files every hour spent on a task that is still going under "work
    with no project", which for a runtime that works while nobody is watching
    is the normal state of the most recent day of an absence. So the active
    records are read too, and they win: a task in both is one that closed
    while this was being built.
    """
    from tesseract.orchestrator.autonomy import agenda_history
    from tesseract.orchestrator.autonomy.agenda_store import AgendaStore

    out: dict[str, str] = {}
    for row in agenda_history.closed_since(None):
        task_id = str(row.get("id") or "")
        if task_id:
            out[task_id] = str(row.get("project_id") or "")
    for item in AgendaStore().iter_active():
        out[item.id] = item.project_id
    return out


def _project_of(project_id: str) -> Any | None:
    if not project_id:
        return None
    from tesseract.orchestrator.projects.store import ProjectStore

    try:
        return ProjectStore().get(project_id)
    except Exception:
        log.debug("return note: could not read project %s", project_id, exc_info=True)
        return None


# ── small shared shapes ────────────────────────────────────────────────


def _verdict(verification_by: str, evidence: str) -> str:
    if verification_by == "gate":
        said = "The project's checks ran and passed"
    elif verification_by == "model":
        said = "Closed on the assistant's own word"
    else:
        return "Nothing recorded who decided it was done"
    return f"{said}: {evidence}" if evidence else said


def _bullets(text: str) -> list[str]:
    return [
        line.strip() for line in text.splitlines() if line.strip().startswith(("- ", "* "))
    ]


def _parse(stamp: str | None) -> datetime | None:
    from tesseract.lib.clock import parse_stamp

    parsed = parse_stamp(stamp)
    return parsed.astimezone(timezone.utc) if parsed is not None else None


def _relative(path: Path) -> str:
    from tesseract.paths import home_dir

    try:
        return path.relative_to(home_dir()).as_posix()
    except ValueError:
        return path.name


def _money(amount: float) -> str:
    return f"${amount:.2f}"


def _when(moment: datetime) -> str:
    return to_local(moment).strftime("%A %d %B, %H:%M")


def _how_long(span: timedelta) -> str:
    hours = max(0, int(span.total_seconds() // 3600))
    if hours < 1:
        return "less than an hour"
    if hours < 48:
        return "1 hour" if hours == 1 else f"{hours} hours"
    return f"{hours // 24} days"


__all__ = ["ABSENT_AFTER", "Line", "Section", "is_owed", "owed", "render"]
