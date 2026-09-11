"""How a playbook did, per revision, over a window.

The usage log says a playbook was read, and which revision. The turn records
say how the turn it was read in ended. The join between them is a function
that already exists: a turn id is minted from the session id
(`turns/manifest.py::turn_session_id`), and a load row carries that session
id and the moment it was read. So a load is joined to the closed turn of the
same session whose span contains that moment, and the turn's own closing
outcome is the playbook's.

**Measured against the version, not the name.** The point of a revision is
that a later one which measures worse can be told apart from the one it
replaced, and returned to. Two revisions of one playbook are two rows here.

**Nothing here invents a number.** A load with no closed turn to join to is
counted as unjoined and is not a success or a failure. A revision no turn
graded has no `trouble` to compare, whether that is because it was never read
or because nothing that read it could be graded, and `worse_than` says so with
`None` rather than a ratio over nothing.

**And it will not take the assistant's word for how a turn went.** A turn
that closed its task on the model's own sentence rather than on a project's
checks is the model grading itself, so it is counted `ungraded`: joined, and
neither a success nor a failure. It is the same refusal as `unjoined`, one
step further in — the turn is there and its outcome is not evidence. A turn
that closed no task at all is unaffected, which is nearly all of them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Iterable

from tesseract.orchestrator.turns.manifest import read_step_name, turn_session_id

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnFact:
    """One turn a revision was read in, and everything about that turn a
    reader has to be able to de-duplicate.

    The outcome is here rather than only in `Reuse`'s counters because a turn
    that read v1 and then v2 of one playbook is ONE turn that closed ONCE. A
    caller adding two revisions up has to be able to see that, and a pair of
    pre-summed integers cannot show it.
    """

    run_id: str
    calls: int
    #: None where the cost ledger was not read. Not zero: those are different
    #: answers and only the second one means it was cheap.
    cost_usd: float | None
    #: `succeeded`, `failed`, or `ungraded` when the turn closed its task on
    #: the assistant's own word and so graded itself.
    outcome: str


@dataclass(frozen=True)
class Reuse:
    """One revision's record over the window."""

    version: str
    loads: int = 0
    #: Loads with no closed turn of that session around them: an open turn,
    #: a turn outside the window, or a read from somewhere that records no
    #: turn. Neither a success nor a failure.
    unjoined: int = 0
    corrections: int = 0
    #: Reads of the same revision after the first inside one turn. The
    #: assistant going back to the procedure mid-task is the cheapest sign
    #: that the procedure did not carry it through.
    retries: int = 0
    #: One entry per DISTINCT turn this revision was read in. The three
    #: numbers below are derived from it rather than accumulated, because a
    #: caller adding up several revisions has to be able to drop a turn that
    #: appears in two of them, and a pre-summed integer cannot be
    #: de-duplicated after the fact.
    #:
    #: **These are the TURN's numbers, not the playbook's**, and the two
    #: cannot be separated by anything on disk: a turn does the work the
    #: playbook describes and everything else the turn was for, and no
    #: record says which call belongs to which. So this answers "what did
    #: the turns that consulted this cost", which is the honest question,
    #: and every surface that shows it says so. Reading it as the price of
    #: the playbook would also be reading it against selection: a playbook
    #: is consulted on the hard tasks and skipped on the easy ones.
    turn_facts: tuple[TurnFact, ...] = ()

    @property
    def succeeded(self) -> int:
        """Turns it was read in that closed `succeeded`.

        Counted off the turns rather than kept beside them. Held as its own
        integer it was a second source for a number the turns already
        answered, and the two could disagree: a caller folding two revisions
        de-duplicated the turns and then added the integers, so one turn that
        read both graded itself twice.
        """
        return sum(1 for f in self.turn_facts if f.outcome == "succeeded")

    @property
    def failed(self) -> int:
        """Turns it was read in that closed any other way."""
        return sum(1 for f in self.turn_facts if f.outcome == "failed")

    @property
    def ungraded(self) -> int:
        """Turns it was read in that closed their task on the model's own
        word. The turn says `succeeded` because the assistant said the task
        was done, which is the one thing this measurement may not believe."""
        return sum(1 for f in self.turn_facts if f.outcome == "ungraded")

    @property
    def turns(self) -> int:
        """How many distinct turns the two numbers below are over, so a
        reader can see the sample rather than infer it from `loads`."""
        return len(self.turn_facts)

    @property
    def turn_calls(self) -> int:
        return sum(fact.calls for fact in self.turn_facts)

    @property
    def turn_cost_usd(self) -> float | None:
        """What those turns cost, or None when that was not measured.

        None and 0.0 are different answers and only the second one means it
        was cheap. A ledger that is not there, or a caller that did not ask
        for one, would otherwise present as a turn that spent nothing, and
        the operator's keep-or-drop reads that as the playbook being free.
        """
        costs = [fact.cost_usd for fact in self.turn_facts]
        if not costs or any(c is None for c in costs):
            return None
        return round(sum(c for c in costs if c is not None), 6)

    @property
    def trouble(self) -> float | None:
        """Failed turns and corrections over loads, or None if nothing graded it.

        Three things have to hold at once, and only the first two used to.

        1. It is a rate per read, so two revisions can be compared.
        2. A revision nobody read has nothing to compare, which is `loads`.
        3. **A revision no turn could grade has nothing to compare either.** A
           read whose turn is `unjoined` or `ungraded` sits in `loads` and can
           never reach the numerator, so a revision read only in those turns
           scored a perfect 0.0 over no evidence at all. `worse_than` read that
           as a record to beat and `skill_refinement` retires a revision that
           measures worse than its predecessor, so a revision three people had
           actually seen work could be withdrawn in favour of one nobody had
           ever measured. `unjoined` has done this since it existed; `ungraded`
           joined it.

        Corrections alone do not make a comparison. A correction says something
        went wrong after a read; it can never say the revision worked, so it
        cannot anchor a "better than" in either direction. It stays in the
        numerator, where it says how much trouble a read led to.
        """
        if not self.loads:
            return None
        if not (self.succeeded or self.failed):
            return None
        return (self.failed + self.corrections) / self.loads

    def as_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "loads": self.loads,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "unjoined": self.unjoined,
            "ungraded": self.ungraded,
            "corrections": self.corrections,
            "retries": self.retries,
            "trouble": self.trouble,
            "turns": self.turns,
            "turn_calls": self.turn_calls,
            "turn_cost_usd": self.turn_cost_usd,
        }


@dataclass(frozen=True)
class ClosedTurn:
    #: The record's own id, which is what the cost ledger keys its rows on.
    run_id: str
    session_id: str
    started_at: datetime
    completed_at: datetime
    succeeded: bool
    #: Tool calls in this turn. Counted here because the walk already reads
    #: every stage row, and a second walk of the same fortnight to count them
    #: is the expensive half done twice.
    calls: int = 0
    #: The turn closed a task, and the evidence was the assistant's sentence
    #: rather than a project's checks. False when it closed no task.
    self_graded: bool = False


def closed_turns(root: Path, *, since: datetime, until: datetime) -> list[ClosedTurn]:
    """Every closed turn record whose end falls in the window.

    Reads the day directories the window can touch and nothing else; `open/`
    is skipped because an open turn has not ended and cannot be joined to.
    """
    if not root.is_dir():
        return []
    found: list[ClosedTurn] = []
    for day_dir in sorted(p for p in root.iterdir() if p.is_dir() and p.name != "open"):
        try:
            day = datetime.fromisoformat(day_dir.name).date()
        except ValueError:
            continue
        if day < since.date() or day > until.date():
            continue
        for path in day_dir.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("playbook reuse: unreadable turn record at %s", path)
                continue
            turn = _closed_turn(raw)
            if turn is None or turn.completed_at < since or turn.completed_at > until:
                continue
            found.append(turn)
    return found


def _closed_turn(raw: dict[str, Any]) -> ClosedTurn | None:
    turn_id = str(raw.get("run_id") or "")
    started = _moment(raw.get("started_at"))
    ended = _moment(raw.get("completed_at"))
    if not turn_id or started is None or ended is None:
        return None
    outcome = ""
    calls = 0
    for row in raw.get("stages") or []:
        kind, name = read_step_name(str(row.get("stage") or ""))
        if kind == "turn" and name == "end":
            outcome = str(row.get("outcome") or "")
        elif kind == "tool":
            calls += 1
    if not outcome:
        return None
    return ClosedTurn(
        run_id=turn_id,
        calls=calls,
        session_id=turn_session_id(turn_id),
        started_at=started,
        completed_at=ended,
        succeeded=outcome == "succeeded",
        self_graded=str(raw.get("task_verification_by") or "") == "model",
    )


def turn_record_around(session_id: str, when: datetime, *, root: Path | None = None) -> dict[str, Any] | None:
    """The closed turn record of `session_id` whose span holds `when`, raw.

    Read off the day directory of `when` and its neighbours, because a turn
    that started before midnight closes after it.
    """
    from tesseract.orchestrator.turns import turns_root as live_turns_root

    base = root if root is not None else live_turns_root()
    if not base.is_dir():
        return None
    for offset in (0, -1, 1):
        day_dir = base / (when + timedelta(days=offset)).date().isoformat()
        if not day_dir.is_dir():
            continue
        for path in day_dir.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            turn_id = str(raw.get("run_id") or "")
            if not turn_id or turn_session_id(turn_id) != session_id:
                continue
            started, ended = _moment(raw.get("started_at")), _moment(raw.get("completed_at"))
            if started is None or ended is None:
                continue
            if started <= when <= ended:
                return raw
    return None


def _moment(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def reuse_by_version(
    name: str,
    *,
    rows: Iterable[dict[str, Any]],
    turns: Iterable[ClosedTurn],
    since: datetime,
    cost_by_turn: Mapping[str, float] | None = None,
) -> dict[str, Reuse]:
    """The record of every revision of `name` read since `since`.

    `cost_by_turn` is the ledger keyed by turn id, and **None means it was
    not read**, which is not the same as every turn costing nothing:
    `Reuse.turn_cost_usd` comes back None so no surface can print an
    unmeasured turn as a free one. A turn missing from a mapping that WAS
    read cost nothing recorded, which is a real zero.
    """
    by_session: dict[str, list[ClosedTurn]] = {}
    by_id: dict[str, ClosedTurn] = {}
    for turn in turns:
        by_session.setdefault(turn.session_id, []).append(turn)
        by_id[turn.run_id] = turn

    counts: dict[str, dict[str, int]] = {}
    seen_in_turn: set[tuple[str, str, datetime]] = set()
    # Once per turn per revision, however many times it was read in that
    # turn: the turn's calls and cost are the turn's, and adding them again
    # for a second read would make going back to the procedure look like
    # twice the work.
    joined: dict[str, dict[str, str]] = {}
    for row in rows:
        if row.get("skill") != name:
            continue
        version = str(row.get("version") or "")
        if not version:
            continue
        when = _moment(row.get("ts"))
        if when is None or when < since:
            continue
        tally = counts.setdefault(
            version,
            {"loads": 0, "unjoined": 0, "corrections": 0, "retries": 0},
        )
        if row.get("outcome") == "correction":
            tally["corrections"] += 1
            continue
        tally["loads"] += 1
        turn = _turn_around(by_session.get(str(row.get("session_id") or ""), ()), when)
        if turn is None:
            tally["unjoined"] += 1
            continue
        key = (version, turn.session_id, turn.started_at)
        outcome = (
            "ungraded" if turn.self_graded
            else "succeeded" if turn.succeeded
            else "failed"
        )
        joined.setdefault(version, {})[turn.run_id] = outcome
        if key in seen_in_turn:
            # The turn has already been graded by the first read. Counting it
            # again would put one turn's outcome in `succeeded` twice, and the
            # panel prints those as "x of y worked": going back to the
            # procedure mid-task would improve the score it is evidence
            # against. The read itself still counts in `loads`.
            tally["retries"] += 1
            continue
        seen_in_turn.add(key)

    out: dict[str, Reuse] = {}
    for version, tally in counts.items():
        facts = tuple(
            TurnFact(
                run_id=run_id,
                calls=by_id[run_id].calls,
                cost_usd=(
                    None if cost_by_turn is None else cost_by_turn.get(run_id, 0.0)
                ),
                outcome=outcome,
            )
            for run_id, outcome in sorted(joined.get(version, {}).items())
            if run_id in by_id
        )
        out[version] = Reuse(version=version, turn_facts=facts, **tally)
    return out



def _turn_around(turns: Iterable[ClosedTurn], when: datetime) -> ClosedTurn | None:
    for turn in turns:
        if turn.started_at <= when <= turn.completed_at:
            return turn
    return None


def worse_than(live: Reuse | None, before: Reuse | None) -> bool | None:
    """Whether `live` measured worse than `before`. None when either has
    nothing measured, because a comparison over nothing is not a finding."""
    if live is None or before is None:
        return None
    if live.trouble is None or before.trouble is None:
        return None
    return live.trouble > before.trouble


def measure_all(
    names: Iterable[str],
    *,
    window_days: int,
    now: datetime | None = None,
    usage_rows: Iterable[dict[str, Any]] | None = None,
    turns_root: Path | None = None,
    with_cost: bool = False,
) -> dict[str, dict[str, Reuse]]:
    """`measure` for several playbooks, over ONE pass of the log and the turns.

    Same arithmetic, read once. `measure` reads the whole usage log and walks
    the turn tree per name, which is right for the one-playbook question the
    refinement job asks and wrong for a panel asking about every playbook at
    once: six of them is six walks of the same fortnight, and the walk is the
    expensive half.

    A name with nothing measured comes back as an empty mapping rather than
    being dropped. That is the interesting half here for the same reason a
    zero row is in `tool_usage.rollup`: a playbook carried on every turn and
    never once read is what the panel exists to show, and a reader that
    returns only what the log holds cannot show one.
    """
    from tesseract.brain.skill_usage import read_usage
    from tesseract.orchestrator.turns import turns_root as live_turns_root

    until = now or datetime.now(timezone.utc)
    since = until - timedelta(days=window_days)
    rows = list(read_usage() if usage_rows is None else usage_rows)
    root = turns_root if turns_root is not None else live_turns_root()
    turns = closed_turns(root, since=since, until=until)
    # Opt-in, because the ledger is read whole and it grows forever. Two
    # callers here never look at the cost: `skill_refinement` asks per
    # candidate inside a loop, so a default-on read would parse the whole
    # file once per playbook per run, and `working_set_review` reads only
    # `loads`. They get None, which their surfaces would print as unmeasured
    # rather than free if they ever printed it.
    costs = cost_by_turn(since) if with_cost else None
    return {
        name: reuse_by_version(
            name, rows=rows, turns=turns, since=since, cost_by_turn=costs
        )
        for name in names
    }


def cost_by_turn(since: datetime) -> dict[str, float] | None:
    """What each turn since `since` spent, keyed by turn id.

    **None means the ledger was not read**, and an empty mapping means it was
    read and holds nothing for this window. Those are different answers all
    the way to the screen: a mapping with no entry for a turn prices that
    turn at zero, which is right, and None makes `Reuse.turn_cost_usd` None
    so no surface can print an unread ledger as a turn that spent nothing.
    Returning `{}` for both was the first fix's mistake: the None contract was
    written down here and honoured only on the path where the caller never
    asked for a cost at all.

    **Read backwards and stopped at the window.** The ledger is append-only,
    time-ordered and never rotated, so parsing it whole to answer "the last
    fortnight" costs the file's whole life on every panel request. Walking
    from the end and breaking at the first row older than `since` bounds the
    parse to the window instead. `spent.parse` is still the only thing that
    knows a ledger line's shape; it is handed one line at a time rather than
    reimplemented.

    Never raises. A ledger that cannot be read costs the cost column and
    nothing else, which is this runtime's rule about a break taking one
    capability off the board rather than stopping everything.
    """
    from tesseract.brain.cost import spent as spent_rows
    from tesseract.brain.cost.ledger import configured_log_path

    path = configured_log_path()
    if path is None or not Path(path).is_file():
        return None
    try:
        # ValueError covers UnicodeDecodeError, which a ledger written by
        # something else, or truncated mid-character, raises rather than the
        # OSError a missing file does.
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        log.warning("playbook reuse: could not read the cost ledger", exc_info=True)
        return None

    out: dict[str, float] = {}
    try:
        for line in reversed(lines):
            for row in spent_rows.parse((line,)):
                if row.ts.astimezone(timezone.utc) < since:
                    return out
                if row.turn_id:
                    out[row.turn_id] = out.get(row.turn_id, 0.0) + row.cost_usd
    except Exception:  # noqa: BLE001
        # The parser skips a line it cannot read, so reaching here means
        # something below it broke. Keep what was counted and say so: a
        # partial cost is still worth more than no panel.
        log.warning("playbook reuse: the cost ledger stopped short", exc_info=True)
    return out


def measure(
    name: str,
    *,
    window_days: int,
    now: datetime | None = None,
    usage_rows: Iterable[dict[str, Any]] | None = None,
    turns_root: Path | None = None,
    with_cost: bool = False,
) -> dict[str, Reuse]:
    """`reuse_by_version` over the live usage log and turn tree, for one name.

    One name is the degenerate case of several, so this delegates rather than
    repeating the window arithmetic and the two reads. It had its own copy of
    both, which is two places for a change to `since`/`until` to be made in
    and one place for it to be forgotten.
    """
    return measure_all(
        [name],
        window_days=window_days,
        now=now,
        usage_rows=usage_rows,
        turns_root=turns_root,
        with_cost=with_cost,
    ).get(name, {})


__all__ = [
    "turn_record_around",
    "ClosedTurn",
    "TurnFact",
    "Reuse",
    "closed_turns",
    "cost_by_turn",
    "measure",
    "measure_all",
    "reuse_by_version",
    "worse_than",
]
