"""project_propose — an idea that is not inside an existing project is a card.

The morning works inside projects the operator has already priced. This is the
one door for everything else: a thing worth building that no project covers.
It is not work, and it does not become work here. It becomes a card in the
inbox saying what the idea is, why now, what the first step would be and what
that step would cost, and the operator decides.

**Approving a card creates nothing.** Operator ruling, 2026-09-09: a tap here
means the idea is worth starting, not that its location, its git setup, its
budget and its checks have been settled. Those are `project_new`'s interview,
and it happens in a conversation after the card is accepted. So this tool never
touches the registry and neither does the approve path.

**Two limits, answering two different questions.** One card open at a time is
about the operator's inbox: a second idea filed while the first is undecided is
two decisions where there was one, and neither is more urgent for it. The days
between is about how often the runtime is allowed to have an idea at all
(`agenda.yaml::morning.project_proposal_every_days`), which is the limit that
stops a machine left running for a month filing thirty. Both are read from the
record rather than held in memory, so a restart does not reset either.

The reasoning this rests on is written in `autonomy/mappers/__init__.py`: six
mappers were deleted for turning something OBSERVED into work. An idea is
observed. It gets a card, and a person turns it into work or does not.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, ClassVar, Optional

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.kernel.tools.receipt import Receipt
from tesseract.workspace_events import EventStore, WorkspaceEvent
from tesseract.workspace_events.broadcast import broadcast_workspace_event

logger = logging.getLogger(__name__)

#: The one card this tool files. Decided in the inbox like any other, and its
#: approve path is deliberately the DEFAULT one: no `_commit_` handler, because
#: approving means the idea is accepted and nothing is created.
CARD_KIND = "project_proposal"


def every_days() -> int:
    """How many days must pass between ideas, from `agenda.yaml`.

    Zero is a real answer and means only the one-at-a-time limit stands.
    `morning.morning_setting` is the one reader of that block and raises on a
    missing key, which is what stops a limit on unattended work falling back
    to a number nobody set.
    """
    from tesseract.orchestrator.autonomy.morning import morning_setting

    return morning_setting("project_proposal_every_days")


def too_soon(last_filed: datetime | None, *, days: int, now: datetime) -> str:
    """Why an idea may not be filed yet, or `""`.

    Measured from the last card FILED, whatever became of it. Measuring from
    the last approved one would let a run of declined ideas file daily, which
    is the case the limit exists for: an operator saying no is the signal to
    propose less, not the signal to propose again tomorrow.
    """
    if days <= 0 or last_filed is None:
        return ""
    due = last_filed + timedelta(days=days)
    if now >= due:
        return ""
    waiting = due - now
    hours = int(waiting.total_seconds() // 3600)
    if hours >= 48:
        # Said in the unit a person would use. A seven day window otherwise
        # reports "waits 144 hours", which is arithmetic rather than an answer.
        left = f"{hours // 24} days"
    elif hours >= 1:
        left = f"{hours} hours"
    else:
        left = "less than an hour"
    return (
        f"the last idea was put to the operator on {last_filed.date().isoformat()} "
        f"and one goes up every {days} days, so this one waits {left}. Write it "
        f"in the diary if it is worth keeping"
    )


def _last_filed(store: EventStore) -> datetime | None:
    """When the newest `project_proposal` card was filed, of any status.

    `list_events` already sorts newest first, so this reads the head rather
    than taking a max over the list: two readers of the same ordering that
    disagree about which is newest is the kind of thing nobody finds.

    A store that will not answer reads as never, which lets one card through
    rather than silencing the door on a bad read. The one-at-a-time check runs
    against the same store and is the tighter limit of the two on any day this
    matters.
    """
    try:
        events = store.list_events(kinds=(CARD_KIND,), limit=1)
    except Exception:  # noqa: BLE001 - an unreadable queue is not a full one
        logger.warning("project_propose: the queue could not be read", exc_info=True)
        return None
    if not events:
        return None
    try:
        when = datetime.fromisoformat(str(events[0].ts))
    except ValueError:
        # A stamp nothing can read is not a licence to guess how old it is.
        # `None` files one card, and the one-at-a-time limit still stands.
        logger.warning("project_propose: a card carries an unreadable time")
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _explain(inp: "ProjectProposeInput") -> list[dict[str, Any]]:
    """The card's body, as `ProposalBody` reads it.

    Built from the same input the payload's own fields come from, in one place,
    so the pane and a later reader cannot disagree about what was proposed.
    `working_set_review::_explain` and `runtime_tuning::_explain` are the same
    function for their own cards, and this is the third.
    """
    return [
        {"title": "What it would be", "lines": [inp.goal.strip()]},
        {"title": "Why now", "lines": [inp.why_now.strip()]},
        {
            "title": "The first step",
            "lines": [
                inp.first_step.strip(),
                f"about ${inp.estimated_cost_usd:.2f}",
            ],
        },
        {
            "title": "What accepting this does",
            "lines": [
                "It says the idea is worth starting. It creates nothing.",
                "Where it lives, whether it is a git repository, what it may "
                "spend a day and what checks decide its work are settled in a "
                "conversation after this.",
            ],
        },
    ]


class ProjectProposeInput(BaseModel):
    name: str = Field(
        min_length=1,
        max_length=80,
        description="What the project would be called, in a few words.",
    )
    goal: str = Field(
        min_length=1,
        max_length=500,
        description="What it would be, in one sentence a person can picture.",
    )
    why_now: str = Field(
        min_length=1,
        max_length=1000,
        description=(
            "What you saw that makes this worth doing, and where you saw it. "
            "A reason with no record behind it is an idea nobody can check."
        ),
    )
    first_step: str = Field(
        min_length=1,
        max_length=500,
        description=(
            "The first thing you would do, small enough to finish and to say "
            "when it is done."
        ),
    )
    estimated_cost_usd: float = Field(
        ge=0,
        description="What you expect that first step to cost in dollars.",
    )


class ProjectProposeTool(Tool):
    # The card is the gate, so filing one is the safe act. Held at `ask` on a
    # shipped install anyway, because an inbox card the operator did not ask
    # for is still something arriving on their screen; `free` relaxes it to
    # auto through the baseline, which is the mode the unattended path is for.
    default_posture = "ask"

    risk_class: ClassVar[str] = "propose"

    group: ClassVar[str] = "projects"
    summary: ClassVar[str] = "Put an idea for a new project to the operator, as a card."
    use_when: ClassVar[str] = (
        "Use when something is worth building that no existing project covers. "
        "Say what it is, what you saw that makes it worth doing, what the "
        "first step is and what that step would cost. It creates nothing: the "
        "operator decides, and starting it is a conversation after that."
    )
    not_when: ClassVar[str] = (
        "for work inside a project that already exists, which is "
        "`task_propose`; to start a project the operator has already agreed "
        "to, which is `project_new`; for a directory that exists on disk, "
        "which is `project_link`."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "record"
    recovery_behaviour: ClassVar[str] = "queryable"

    def __init__(
        self,
        store: EventStore,
        *,
        app_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._store = store
        self._app_provider = app_provider

    @property
    def name(self) -> str:
        return "project_propose"

    @property
    def input_schema(self) -> type[BaseModel]:
        return ProjectProposeInput

    def is_concurrency_safe(self) -> bool:
        return False

    def is_read_only(self) -> bool:
        return False

    def ask_reason(self, validated: Any) -> str:
        """What the operator is agreeing to see, readable on a phone: the idea
        and its first step, never an id."""
        inp = validated if isinstance(validated, ProjectProposeInput) else None
        if inp is None:
            return "put an idea for a new project to you"
        return (
            f"put an idea to you: {inp.name.strip()}. {inp.goal.strip()} "
            f"First step: {inp.first_step.strip()}"
        )

    def _already_waiting(self) -> ToolResult:
        """One sentence, said from both places that can reach it.

        The early read and the write both refuse for the same reason, and two
        wordings for one refusal is two things for the operator to work out.
        """
        return ToolResult(
            output=(
                "There is already an idea waiting for the operator to decide. "
                "One at a time: a second one now is two decisions where there "
                "was one. Wait for the first to be answered."
            ),
            is_error=True,
            caller_error=True,
            metadata={"refused": "one_already_open"},
        )

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, ProjectProposeInput)
            else ProjectProposeInput(**tool_input.model_dump())
        )

        # The one-at-a-time limit is enforced by the WRITE, further down, not
        # by a check here: `append_if_none_pending` holds one lock across
        # looking and writing, so two callers proposing at the same moment
        # cannot both see an empty queue. This early read is only so the
        # cadence below is not computed for a call that is already refused.
        if self._store.one_is_pending(CARD_KIND):
            return self._already_waiting()

        try:
            days = every_days()
        except (OSError, KeyError, ValueError) as exc:
            logger.exception("project_propose: the cadence could not be read")
            return ToolResult(
                output=(
                    f"How often an idea may be put up could not be read "
                    f"({exc}), so nothing was filed."
                ),
                is_error=True,
            )
        waiting = too_soon(
            _last_filed(self._store), days=days, now=datetime.now(timezone.utc)
        )
        if waiting:
            return ToolResult(
                output=f"Not filed: {waiting}.",
                is_error=True,
                caller_error=True,
                metadata={"refused": "too_soon"},
            )

        summary = "\n".join(
            (
                inp.goal.strip(),
                "",
                f"Why now: {inp.why_now.strip()}",
                f"First step: {inp.first_step.strip()}",
                f"That step would cost about ${inp.estimated_cost_usd:.2f}.",
                "",
                "Accepting this creates nothing. It says the idea is worth "
                "starting, and where it lives, whether it is a git repository, "
                "what it may spend and what checks decide its work are settled "
                "in a conversation after that.",
            )
        )
        event = WorkspaceEvent.new(
            kind=CARD_KIND,
            source="agent",
            title=f"An idea: {inp.name.strip()}"[:200],
            summary=summary[:1200],
            payload={
                "name": inp.name.strip(),
                "goal": inp.goal.strip(),
                "why_now": inp.why_now.strip(),
                "first_step": inp.first_step.strip(),
                "estimated_cost_usd": round(float(inp.estimated_cost_usd), 4),
                # What the cockpit pane draws. `ProposalBody` is the shared
                # body for every proposal card and authors no sentence of its
                # own, so the words are built here beside the fields they
                # render, in one expression over the same input.
                "explain": _explain(inp),
                "session_id": context.session_id,
            },
            priority=3,
            author_id="agent",
            author_display="Agent",
        )
        try:
            written = self._store.append_if_none_pending(event)
        except OSError as exc:
            logger.exception("project_propose: the card could not be written")
            return ToolResult(
                output=f"The idea could not be put up ({exc}), so nothing is waiting.",
                is_error=True,
            )
        if not written:
            # Another caller filed one between the read above and this write.
            # The store refused rather than letting both land, which is the
            # whole reason the check and the write are one call.
            return self._already_waiting()
        if self._app_provider is not None:
            app = self._app_provider()
            if app is not None:
                await broadcast_workspace_event(app, event)
        return ToolResult(
            output=(
                f"Put to the operator as an idea: {inp.name.strip()}. Nothing "
                f"has been created. When they accept it, start it with "
                f"`project_new` and ask them where it lives, whether it is a "
                f"git repository, what it may spend a day and what checks "
                f"decide its work."
            ),
            receipt=Receipt(
                kind="record",
                id=event.event_id,
                locator=str(self._store.events_path),
            ),
            metadata={"event_id": event.event_id, "created": False},
        )


__all__ = [
    "CARD_KIND",
    "ProjectProposeInput",
    "ProjectProposeTool",
    "every_days",
    "too_soon",
]
