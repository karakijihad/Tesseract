"""What the turn carries, judged against what it actually reached for.

The working set is a spending dial: every tool named in ``working_set.yaml::
core`` costs its schema on every turn, and every playbook named in
``workspace/skills/carried.txt`` costs its steps. Both gauges have existed for
a while and neither has ever moved anything. This is the stage that reads them
and proposes a change.

**It proposes and never writes.** ``core:`` is the operator's half of a file
whose other half is generated, and the whole point of that split is that a
person chooses the names. A job that edited it would take the one decision the
split exists to protect, so what it produces is a card carrying the change and
the window it was computed over. A test asserts both files are byte-identical
after a run that proposed something.

**No model is involved, and that is the design rather than a shortfall.** The
proposal IS the arithmetic: a tool reached for through a search in four
separate sessions should be carried, one carried and never called should not.
`skill_refinement` asks a model to write a revised procedure because a
procedure is prose; a working set is a list of names, and there is nothing here
for a model to author.

Three things it must hold at once, and each has been the way to get this wrong:

1. **Silence is not evidence.** An empty ledger makes every carried tool look
   unused. Proposing to strip the core because nothing was logged would be the
   worst possible reading of the least possible data, so the run refuses to
   judge until the window holds ``min_calls_to_judge`` calls, and says so.
2. **Nothing to change means no card**, not a card saying nothing changed
   (Governance §3).
3. **The floor is not a candidate.** ``tool_search`` is how everything off the
   list is reached and ``playbook_search`` is its twin. Proposing to drop
   either would turn a spending dial into a capability cut.

Fired on volume rather than a clock, the way `skill_refinement` is: a cadence
reads two data points as readily as two hundred, and what decides whether the
dial can be judged is how much has been logged since the last look.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tesseract.orchestrator.outcome import RunOutcome
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.tasks import _card
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

#: The one card this stage files. Decided in the inbox like any other, and the
#: approve path is `routes/workspace.py::_commit_working_set_proposal`.
CARD_KIND = "working_set_proposal"


class WorkingSetReviewJob(BaseJob):
    uses_llm = False

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            cfg = ctx.config or {}
            window_days = int(cfg.get("window_days", 14))
            min_sessions = int(cfg.get("min_sessions_to_carry", 4))
            # A playbook has no session count of its own: the gauge counts
            # loads. Its own key, so the two thresholds can be set apart and
            # neither reads as the other's unit.
            min_loads = int(cfg.get("min_loads_to_carry", 4))
            min_calls = int(cfg.get("min_calls_to_judge", 50))
            max_proposals = int(cfg.get("max_proposals", 8))

            store = _resolve_store(ctx)
            if _card_already_waiting(store):
                return self._closed(
                    ctx, t0, RunOutcome.SKIPPED_NO_WORK,
                    "a working set proposal is already waiting to be decided",
                    {"filed": 0},
                )

            reading = await _read(ctx, window_days)
            if reading.calls < min_calls:
                # Rule 1. Not a failure and not a proposal: the dial cannot be
                # judged yet, and saying so is the honest closed state.
                return self._closed(
                    ctx, t0, RunOutcome.SKIPPED_NO_WORK,
                    f"only {reading.calls} tool calls logged in {window_days} days, "
                    f"which is under the {min_calls} it takes to judge what is carried",
                    {"filed": 0, "calls": reading.calls},
                )

            declined = _already_declined(store, ctx.fired_at)
            proposal = _propose(
                reading,
                min_sessions,
                max_proposals,
                min_loads=min_loads,
                declined=declined,
            )
            if not proposal.any():
                # Rule 2.
                return self._closed(
                    ctx, t0, RunOutcome.SKIPPED_NO_WORK,
                    "what is carried matches what was used",
                    {"filed": 0, "calls": reading.calls},
                )

            filed = await _file_card(
                ctx, store, proposal, reading, window_days, declined
            )
            if not filed:
                return self._closed(
                    ctx, t0, RunOutcome.DEGRADED,
                    "the proposal could not be filed as a card, so nobody was asked",
                    {"filed": 0},
                )
            return self._closed(
                ctx, t0, RunOutcome.SUCCEEDED, "",
                {"filed": 1, "calls": reading.calls, **proposal.as_json()},
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("working_set_review crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )

    def _closed(
        self,
        ctx: JobContext,
        t0: float,
        outcome: RunOutcome,
        reason: str,
        payload: dict[str, Any],
    ) -> JobResult:
        return JobResult(
            job_name=ctx.job_name,
            run_id=ctx.run_id,
            ok=True,
            detail=reason or f"filed {payload.get('filed', 0)} proposal",
            outcome=outcome,
            outcome_reason=reason,
            payload=payload,
            duration_ms=(time.monotonic() - t0) * 1000.0,
        )


class Reading:
    """What the two gauges hold over the window, and what is carried now."""

    def __init__(
        self,
        tools: list[dict[str, Any]],
        calls: int,
        core: set[str],
        locked: set[str],
        playbooks: list[dict[str, Any]],
        roster: bool,
    ) -> None:
        self.tools = tools
        self.core = core
        self.locked = locked
        self.playbooks = playbooks
        self.roster = roster
        #: The WHOLE window, not the candidate slice. See `_tools`.
        self.calls = calls


class Proposal:
    def __init__(self) -> None:
        self.carry: list[dict[str, Any]] = []
        self.drop: list[dict[str, Any]] = []
        self.drop_playbooks: list[dict[str, Any]] = []
        self.carry_playbooks: list[dict[str, Any]] = []

    def any(self) -> bool:
        return bool(
            self.carry or self.drop or self.drop_playbooks or self.carry_playbooks
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "carry": self.carry,
            "drop": self.drop,
            "drop_playbooks": self.drop_playbooks,
            "carry_playbooks": self.carry_playbooks,
        }


async def _read(ctx: JobContext, window_days: int) -> Reading:
    import asyncio

    from tesseract.brain import tool_usage
    from tesseract.config.working_set import UNDROPPABLE, load_core_tool_names

    registry = ctx.app.get("tool_registry") if ctx.app is not None else None
    roster = sorted(registry.tools) if registry is not None else []
    # A custom tool is promoted in its own machine-local file, so it is not a
    # candidate for a change to the shipped list.
    shipped = {
        name
        for name in roster
        if getattr(registry.tools[name], "origin", "shipped") != "custom"
    } if registry is not None else set()

    def _tools() -> tuple[list[dict[str, Any]], int]:
        """The candidates, and the whole window's call count beside them.

        Two numbers because they answer different questions. Candidacy is
        about the SHIPPED list, so an MCP or custom tool is filtered out of
        it. Whether the ledger is instrumented enough to judge anything is
        about the window, and counting only the filtered slice reported "not
        enough calls" over a busy ledger on a machine whose work runs through
        tools this list will never name.
        """
        rows = tool_usage.rollup(window_days, roster)
        return (
            [r for r in rows if not shipped or r["tool"] in shipped],
            sum(int(r["calls"]) for r in rows),
        )

    tools, window_calls = await asyncio.to_thread(_tools)
    try:
        core = set(load_core_tool_names())
    except (OSError, ValueError, KeyError) as exc:
        log.warning("working_set_review: cannot read the working set: %s", exc)
        core = set()

    return Reading(
        tools=tools,
        calls=window_calls,
        core=core,
        # Rule 3: the two doors to everything not carried, from the one
        # constant the approval route reads too.
        locked=set(UNDROPPABLE),
        playbooks=await asyncio.to_thread(_playbook_reading, window_days, ctx.fired_at),
        roster=registry is not None,
    )


def _playbook_reading(window_days: int, now: datetime) -> list[dict[str, Any]]:
    """Every live playbook, how often it was read, and whether it is carried.

    **Every one, not only the carried ones.** Measuring the carried set alone
    could answer one direction of the question and not the other: a playbook
    read constantly while off the list pays a `playbook_search` every time and
    had no way onto the card. The tool half has carried both directions since
    it was written, and the asymmetry here was an omission rather than a
    ruling.
    """
    from tesseract.brain.playbook_reuse import measure_all
    from tesseract.brain.playbook_set import carried_path, load_carried_names, skills_dir
    from tesseract.brain.skills import load_skills

    carried = load_carried_names(carried_path())
    live = sorted(
        e.name
        for e in load_skills(skills_dir())
        if e.is_playbook and e.status != "retired"
    )
    if not live:
        return []
    measured = measure_all(live, window_days=window_days, now=now)
    return [
        {
            "playbook": name,
            "loads": sum(r.loads for r in measured.get(name, {}).values()),
            "carried": name in carried,
        }
        for name in live
    ]


def _propose(
    reading: Reading,
    min_sessions: int,
    max_proposals: int,
    *,
    min_loads: int = 4,
    declined: set[tuple[str, str, str]] | None = None,
) -> Proposal:
    """The whole judgement, and it is arithmetic.

    Carry: reached for in `min_sessions` separate sessions while not on the
    list, so every one of those sessions paid a `tool_search` first. Ranked by
    distinct sessions rather than calls, for the reason the ledger is: one loop
    calling a tool four hundred times is one session's worth of evidence.

    Drop: on the list and not called once in the window, so it has cost its
    schema on every turn and bought nothing.
    """
    out = Proposal()
    refused = declined or set()
    for row in reading.tools:
        name = str(row["tool"])
        if name in reading.locked:
            continue
        sessions, calls = int(row["sessions"]), int(row["calls"])
        if name not in reading.core and sessions >= min_sessions and (
            ("tool", "carry", name) not in refused
        ):
            out.carry.append({"tool": name, "sessions": sessions, "calls": calls})
        elif name in reading.core and calls == 0 and reading.roster and (
            ("tool", "drop", name) not in refused
        ):
            # Only with a roster: without one the ledger's own keys are all
            # there is, and every carried tool absent from it would read as
            # unused when the truth is that nothing could enumerate them.
            out.drop.append({"tool": name, "sessions": 0, "calls": 0})

    out.carry.sort(key=lambda r: (-r["sessions"], -r["calls"], r["tool"]))
    out.drop.sort(key=lambda r: r["tool"])
    out.carry = out.carry[:max_proposals]
    out.drop = out.drop[:max_proposals]
    out.drop_playbooks = [
        {"playbook": row["playbook"], "loads": 0}
        for row in reading.playbooks
        if row["carried"] and int(row["loads"]) == 0
        and ("playbook", "drop", row["playbook"]) not in refused
    ][:max_proposals]
    # The direction the tool half always had: read often while off the list,
    # so every one of those reads paid a `playbook_search` first. Ranked by
    # loads, because a playbook has no session count of its own.
    carry_playbooks = sorted(
        (row for row in reading.playbooks
         if not row["carried"] and int(row["loads"]) >= min_loads
         and ("playbook", "carry", row["playbook"]) not in refused),
        key=lambda r: (-int(r["loads"]), r["playbook"]),
    )
    out.carry_playbooks = [
        {"playbook": r["playbook"], "loads": int(r["loads"])}
        for r in carry_playbooks
    ][:max_proposals]
    return out


def _explain(
    proposal: Proposal,
    window_days: int,
    calls: int,
    declined: set[tuple[str, str, str]] | None = None,
) -> list[dict[str, Any]]:
    """The whole of what the card says, written here.

    **No explanatory copy about a proposal lives in TSX.** The pane renders
    these titles and lines and authors nothing, so what the operator reads and
    what the arithmetic did cannot drift apart, and a threshold changed in
    config does not leave a sentence in a component describing the old one.
    One line per name rather than one per group: the evidence differs per name
    and a joined list hides which tool the number belongs to.
    """
    sections: list[dict[str, Any]] = []
    if proposal.carry:
        sections.append({
            "title": "Carry these on every turn",
            "lines": [
                f"{r['tool']}: looked up first in {r['sessions']} separate "
                f"{'session' if r['sessions'] == 1 else 'sessions'}, "
                f"{r['calls']} {'call' if r['calls'] == 1 else 'calls'} in all. "
                "Carrying it saves that search every time."
                for r in proposal.carry
            ],
        })
    if proposal.drop:
        sections.append({
            "title": "Stop carrying these",
            "lines": [
                f"{r['tool']}: described on every turn and not called once in "
                "the window. It stays available, one tool_search away."
                for r in proposal.drop
            ],
        })
    if proposal.carry_playbooks:
        sections.append({
            "title": "Carry these playbooks on every turn",
            "lines": [
                f"{r['playbook']}: read {r['loads']} times while off the list, "
                "so each of those turns looked it up first. Carrying it brings "
                "its version, its status and when to use it into every turn."
                for r in proposal.carry_playbooks
            ],
        })
    if proposal.drop_playbooks:
        sections.append({
            "title": "Stop carrying these playbooks",
            "lines": [
                f"{r['playbook']}: when to use it rides every turn and it was "
                "never read. It stays available, one playbook_search away."
                for r in proposal.drop_playbooks
            ],
        })
    read_from = [
        f"{calls} tool calls over {window_days} days, ranked by how many "
        "separate sessions reached for each rather than by raw calls.",
    ]
    if proposal.drop or proposal.drop_playbooks:
        # Only where there is a zero to explain. Nothing records when a name
        # joined the list, so the honest sentence is that the window may
        # predate it: a name added recently and used since shows those calls
        # like any other, and claiming it could not was a guarantee the
        # arithmetic does not make.
        read_from.append(
            f"A name you added part-way through these {window_days} days is "
            "judged on the whole window, so a low count may be the window "
            "being older than the name rather than the name going unused."
        )
    read_from.append(
        "Nothing has been changed. Approving applies it. You can also move "
        "any name yourself in Conscience, under Usage."
    )
    sections.append({"title": "What this was read from", "lines": read_from})
    # **The hold is said out loud, or it is a filter nobody can inspect.**
    # This phase's own rule is that the panel says what it proposed and why;
    # a suppression that silently removes a name from consideration for three
    # months is the same defect wearing the opposite sign. An operator who
    # declined something and then wondered why it never came up again had no
    # surface that would tell them.
    held = sorted({name for _kind, _direction, name in (declined or set())})
    if held:
        sections.append({
            "title": "Held back because you said no before",
            "lines": [
                "Not proposed again for " + str(DECLINED_HOLD_DAYS) + " days after "
                "you decline them: " + ", ".join(held) + ".",
                "You can still move any of them yourself in Conscience, under "
                "Usage, and doing that is what tells this to stop holding off.",
            ],
        })
    return sections


def _summary(proposal: Proposal, window_days: int, calls: int) -> str:
    """The one line the inbox row shows, before the card is opened."""
    counts: list[str] = []
    if proposal.carry:
        counts.append(f"carry {len(proposal.carry)} more")
    if proposal.drop:
        counts.append(f"drop {len(proposal.drop)}")
    if proposal.carry_playbooks:
        counts.append(f"carry {len(proposal.carry_playbooks)} playbook")
    if proposal.drop_playbooks:
        counts.append(f"drop {len(proposal.drop_playbooks)} playbook")
    return (
        "Read from " + f"{calls} tool calls over {window_days} days: "
        + ", ".join(counts) + ". Nothing has been changed yet."
    )


def _declared_kinds(proposal: Proposal) -> list[str]:
    """Which of the runtime's declared proposal kinds this card carries.

    The four lists are two questions asked of two subjects: carrying more, and
    carrying less, of a tool or of a playbook. `scheduler/proposals.py` is
    where what may be proposed at all is declared, and this job is one of two
    producers drawing from it, so the keys go ON the card rather than being
    inferred from the payload's shape by whoever reads it next.

    `filed` raises rather than returning a flag. A card is the wrong place to
    discover that a producer invented a kind: the run is over by then and the
    operator is looking at it.
    """
    from tesseract.scheduler import proposals

    keys: list[str] = []
    if proposal.carry or proposal.carry_playbooks:
        keys.append(proposals.filed("carry_more").key)
    if proposal.drop or proposal.drop_playbooks:
        keys.append(proposals.filed("carry_less").key)
    return keys


async def _file_card(
    ctx: JobContext,
    store: Any,
    proposal: Proposal,
    reading: Reading,
    window_days: int,
    declined: set[tuple[str, str, str]] | None = None,
) -> bool:
    from tesseract.workspace_events import WorkspaceEvent

    changes = sum(
        len(rows) for rows in proposal.as_json().values() if isinstance(rows, list)
    )
    event = WorkspaceEvent.new(
        kind=CARD_KIND,
        source="agent",
        title=(
            f"{changes} change to what every turn carries"
            if changes == 1
            else f"{changes} changes to what every turn carries"
        ),
        summary=_summary(proposal, window_days, reading.calls),
        payload={
            **proposal.as_json(),
            "kinds": _declared_kinds(proposal),
            "explain": _explain(proposal, window_days, reading.calls, declined),
            "window_days": window_days,
            "calls": reading.calls,
            "computed_at": ctx.fired_at.isoformat(),
        },
    )
    try:
        store.append_event(event)
    except Exception:
        log.exception("working_set_review: append card failed")
        return False
    await _broadcast(ctx, event)
    return True


def _card_already_waiting(store: Any) -> bool:
    return _card.one_waiting(store, CARD_KIND)


#: How long a `no` holds. A rejection has to outlive the card it was said on,
#: or the same names produce a byte-identical proposal on the next firing and
#: the answer to a question nobody changed their mind about arrives again.
#: It also has to EXPIRE, or the opposite failure sets in silently: a name
#: declined once is never proposed again however far its usage moves, and the
#: dial quietly stops being able to learn about it. Two windows either side of
#: the same mistake, so the quiet one gets a number too.
DECLINED_HOLD_DAYS = 90


def _already_declined(store: Any, now: datetime) -> set[tuple[str, str, str]]:
    """`(kind, direction, name)` rejected recently enough for the no to hold.

    Names, not whole proposals: usage moves, so the next card is rarely the
    same set, and comparing sets would let one new name drag eight declined
    ones back in with it.

    **Keyed by direction, because declining one is not declining the other.**
    "Stop carrying glob" and "start carrying glob" are different questions,
    and a name is normally only a candidate for one of them at a time. They
    both become reachable the moment the operator moves the name by hand
    afterwards, which is exactly when the runtime should be able to speak
    again rather than be held to an answer about the opposite question.

    A card whose `decided_at` cannot be read is treated as EXPIRED rather than
    as holding. The two mistakes are not symmetric: holding forever on an
    unreadable stamp silently removes a name from the dial with nothing on any
    screen saying so, where expiring early costs one card the operator can
    decline again.
    """
    declined: set[tuple[str, str, str]] = set()
    try:
        rows = store.list_events(kinds=(CARD_KIND,), status="rejected")
    except Exception:
        return declined
    cutoff = now - timedelta(days=DECLINED_HOLD_DAYS)
    for ev in rows:
        when = _moment(getattr(ev, "decided_at", None)) or _moment(getattr(ev, "ts", None))
        if when is None or when < cutoff:
            continue
        payload = ev.payload or {}
        for key, field in (
            ("carry", "tool"), ("drop", "tool"),
            ("carry_playbooks", "playbook"), ("drop_playbooks", "playbook"),
        ):
            direction = "carry" if key.startswith("carry") else "drop"
            for row in payload.get(key) or []:
                name = str(row.get(field) or "")
                if name:
                    # Keyed by kind as well, so a tool and a playbook that
                    # happen to share a name do not share a rejection.
                    declined.add((field, direction, name))
    return declined


def _moment(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _resolve_logs_dir(ctx: JobContext) -> Path:
    return _card.logs_dir(ctx)


def _resolve_store(ctx: JobContext) -> Any:
    return _card.store(ctx)


async def _broadcast(ctx: JobContext, event: Any) -> None:
    await _card.announce(ctx, event, who="working_set_review")


__all__ = ["WorkingSetReviewJob", "CARD_KIND"]
