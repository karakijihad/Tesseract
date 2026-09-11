"""The join between a turn and the task it works, written from both ends.

One obligation spans many turns, sessions, restarts and days. The turn's record
says which task it worked (`RunManifest.task_id`) and the task says which turns
worked it (`AgendaItem.turn_ids`, and one `Attempt` per turn that ended), so
neither record is authoritative alone and a reader starting from either finds
the other.

Invariants, held together:
  1. Taking up a task binds BOTH ends in one call, and only from a state the
     conversation may take: `proposed`, `resume_queued`, or already `running`.
     `blocked` and `awaiting_operator` are the operator's to release.
  2. A turn that ends appends one attempt with the outcome the turn ended on,
     the same vocabulary the turn's own record uses. Never twice for one turn.
  3. A turn the last process left open is closed as `truncated` by recovery,
     and the task it was working moves to `resume_queued` with a reason in
     words, keeping its goal, its evidence and its history. What resumes is
     the task, never the half-finished turn.
  4. Nothing here raises into the turn or the boot. The work is the point and
     the record is best effort, the rule every turn write already follows.
  5. Nothing retries a task on its own. The kernel's resume sweep leaves this
     source alone, so a recovered task waits in `resume_queued`, visible, until
     a conversation takes it up. This module records; it does not decide.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    Attempt,
    TransitionActor,
)
from tesseract.orchestrator.outcome import RunOutcome
from tesseract.scheduler.pipeline.manifest import RunManifest

log = logging.getLogger(__name__)

#: States a conversation may take a task up from.
TAKEABLE = frozenset({AgendaStatus.PROPOSED, AgendaStatus.RESUME_QUEUED, AgendaStatus.RUNNING})

INTERRUPTED_REASON = (
    "The turn working this stopped when the app restarted, and nothing was "
    "repeated. Take it up again in a conversation; anything that had an "
    "effect before is on the record and is not done twice."
)


class NotTakeable(ValueError):
    """The item is not a task, or not in a state the conversation may take."""


def why_not_takeable(item: AgendaItem) -> str:
    """The sentence a tool gives back when a task cannot be taken up, with the
    remedy. Empty when it can."""
    if item.source is not AgendaSource.TASK:
        return (
            f"{item.id} is autonomy's own item, not a task. It is worked in the "
            f"background; `agenda_comment` is how to speak to it."
        )
    if item.is_terminal():
        return (
            f"{item.id} is {item.status.value.replace('_', ' ')} and finished. "
            f"Propose a new task if there is more to do."
        )
    if item.status is AgendaStatus.BLOCKED:
        return (
            f"{item.id} is blocked: {item.blocked_reason or 'no reason recorded'}. "
            f"The operator releases it from the Autonomy panel; it cannot be "
            f"taken up from here."
        )
    if item.status is AgendaStatus.AWAITING_OPERATOR:
        return f"{item.id} is waiting on the operator's answer. Nothing to do until it comes."
    if item.status not in TAKEABLE:
        return f"{item.id} is {item.status.value.replace('_', ' ')} and cannot be taken up from there."
    return ""


def take_up(
    item: AgendaItem,
    *,
    turn_id: str,
    bind: "callable | None",
    store: AgendaStore,
) -> AgendaItem:
    """Bind the running turn to the task, both ends, and mark the task running.

    `bind` is the turn's own `TurnRecorder.bind_task`, handed down through the
    tool context; `None` when the caller runs no turn (the REPL, a test), in
    which case the task side is still written and says nothing about a turn.
    """
    refusal = why_not_takeable(item)
    if refusal:
        raise NotTakeable(refusal)
    if turn_id and turn_id not in item.turn_ids:
        item.turn_ids.append(turn_id)
    if item.status is AgendaStatus.RUNNING:
        store.save(item)
    else:
        where = f"in turn {turn_id}" if turn_id else "outside a recorded turn"
        store.transition(item, AgendaStatus.RUNNING, reason=f"taken up {where}")
    if bind is not None and turn_id:
        bind(item.id)
    return item


def last_attempt_was_interrupted(item: AgendaItem) -> bool:
    return bool(item.attempts) and item.attempts[-1].outcome is RunOutcome.TRUNCATED


def note_turn_ended(
    manifest: RunManifest,
    outcome: RunOutcome,
    reason: str = "",
    *,
    store: AgendaStore | None = None,
    by: TransitionActor = "kernel",
) -> AgendaItem | None:
    """Write the turn's end onto the task it was working, if it was working one.

    Returns the item as saved, or `None` when the turn named no task, the task
    cannot be found, or the write failed. Recovery passes `by="recovery"` and
    `outcome=TRUNCATED`, which is the one combination that moves a running
    task to `resume_queued`; a turn that ended on its own leaves the status
    where it is, because one obligation spans many turns.
    """
    if not manifest.task_id:
        return None
    agenda = store or AgendaStore()
    try:
        item = agenda.get(manifest.task_id)
    except Exception:  # noqa: BLE001 - a bad record must not fail the turn
        log.exception("turn %s: could not read task %s", manifest.run_id, manifest.task_id)
        return None
    if item is None:
        log.warning("turn %s named task %s, which does not exist", manifest.run_id, manifest.task_id)
        return None
    if any(a.turn_id == manifest.run_id for a in item.attempts):
        return item
    ended = manifest.completed_at or datetime.now(timezone.utc)
    item.attempts.append(
        Attempt(
            turn_id=manifest.run_id,
            started_at=manifest.started_at,
            ended_at=ended,
            outcome=outcome,
            reason=reason,
        )
    )
    try:
        if (
            by == "recovery"
            and outcome is RunOutcome.TRUNCATED
            and item.status is AgendaStatus.RUNNING
        ):
            # Set on the item the transition below is about to save, so the
            # reference rides that write and a transition that fails takes the
            # unsaved field down with it. There is no half state: this function
            # returns `None` on failure and its one caller reads nothing off
            # the object it did not get.
            item.current_checkpoint = _where_it_stopped(manifest.run_id)
            agenda.transition(
                item, AgendaStatus.RESUME_QUEUED, reason=INTERRUPTED_REASON, by="recovery"
            )
        else:
            agenda.save(item)
    except Exception:  # noqa: BLE001 - see invariant 4
        log.exception("turn %s: could not write its end onto task %s", manifest.run_id, item.id)
        return None
    return item


def _where_it_stopped(run_id: str) -> str | None:
    """The checkpoint this run was standing at, as a reference, or None.

    Written only here, and only at the recovery park, which is the one moment
    the reference means anything: a task moving to `resume_queued` is a task
    whose next reader has to find the record of what it was doing, and every
    other moment in a task's life has a live turn that already knows.

    Stamping it at each boundary instead would be an agenda file rewritten
    several times a turn to hold a value nothing reads until a crash, and the
    id would be stale the instant the turn moved on anyway.

    Best effort, like everything else in this module. A store that will not
    read costs the reference and never the park: the task still resumes, and
    the conversation that takes it up reads its own conversation's record,
    which is where the same rows are.
    """
    try:
        from tesseract.orchestrator.checkpoints import latest_for_run

        row = latest_for_run(run_id)
    except Exception:  # noqa: BLE001 - see invariant 4
        log.exception("turn %s: could not read where it stopped", run_id)
        return None
    return row.checkpoint_id if row is not None else None


__all__ = [
    "INTERRUPTED_REASON",
    "TAKEABLE",
    "NotTakeable",
    "last_attempt_was_interrupted",
    "note_turn_ended",
    "take_up",
    "why_not_takeable",
]
