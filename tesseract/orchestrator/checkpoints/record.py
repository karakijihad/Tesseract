"""What one boundary says, and how a reported one becomes a record.

The shape and nothing else. Everything about WHERE a row lives, when it is
folded, pruned or read is another module here, so a change to the fields
touches one file and a change to the layout touches a different one.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


#: How many of each list a checkpoint keeps. A boundary that reports two hundred
#: remaining items is reporting a plan, not a state, and the tail CC-8 builds
#: from this has to fit in a context window.
LIST_CAP = 20


#: Per string. Long enough for a sentence a person reads, short enough that a
#: model cannot smuggle a transcript into a field meant for a reference.
FIELD_CHARS = 600


#: A boundary written after the conversation reflected. The only kind that
#: existed before the six, so it is the default and every row already on disk
#: reads back as exactly what it was.
CONSOLIDATION = "consolidation"


#: The six places a checkpoint is written, and the set is closed: a kind no
#: reader knows is a row invisible to both of them, which is worse than no row
#: at all because the file still looks written to.
BOUNDARIES: frozenset[str] = frozenset(
    {
        "before_model",      # about to ask the model
        "after_model",       # its answer is recorded
        "before_tool",       # about to make a call that may act
        "after_tool",        # the result and its receipt are recorded
        "awaiting_operator",  # parked on an approval or a question
        CONSOLIDATION,       # the conversation reflected and folded
    }
)


@dataclass(frozen=True)
class Checkpoint:
    """One boundary's answer to "what was I doing, and what happens next".

    Every field is optional and empty is a legitimate value. A model that does
    not know what remains says nothing, and the runtime records that it said
    nothing rather than inventing a next action from the transcript. A guessed
    next action is worse than none: the next context acts on it.
    """

    checkpoint_id: str
    ts: str
    #: Which connection wrote it. Evidence, never a key: see `chat_id`.
    session_id: str
    trigger: str
    outcome: str
    #: Which CONVERSATION this was, and the only field a later reader can find
    #: one by. `session_id` is a connection: every cockpit chat open on one
    #: WebSocket carries the same value and it changes on reload, so looking a
    #: boundary up by it returns whichever chat reflected most recently.
    #: `tool_context.chat_id` is the durable id both surfaces stamp
    #: (`session_model.stamp_chat_id`, `_channel_session`) and it survives a
    #: restart. Empty for a conversation with no durable id, which is a
    #: sub-agent or a synthetic turn, and one of those has nothing to come back
    #: to.
    #:
    #: Defaulted rather than required so a record written before this field
    #: existed still loads. An old line reads as a boundary nothing can be
    #: rebuilt from, which is what it is.
    chat_id: str = ""
    #: Why a `continue` was refused and turned into a `reset`, in one sentence,
    #: or empty when nothing was refused.
    #:
    #: The outcome alone cannot say this. A refused continue and a conversation
    #: that finished its work both end as `reset`, and reading the record back
    #: they would be the same event: "the runtime stopped this because it was
    #: going round" and "the work was done" are the two things a later reader
    #: most needs to tell apart.
    refused: str = ""
    objective: str = ""
    phase: str = ""
    completed: list[str] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)
    next_action: str = ""
    open_questions: list[str] = field(default_factory=list)
    blocked_by: str = ""
    artifacts: list[str] = field(default_factory=list)

    #: What `artifacts` are relative to, written by the RUNTIME and never by
    #: the model.
    #:
    #: A model writing paths from inside a task writes them the way it has been
    #: saying them, which is project-relative, and one package mixed a path
    #: under `tools/` with one under `workshop/projects/<name>/`, each relative
    #: to a different root. Neither opens from anywhere. The next context then
    #: had a list of files it could not read and did the only thing left, which
    #: was to search for the project it was already standing in.
    #:
    #: The runtime knows this without asking: `ProjectStore().active()` has the
    #: root, and its `last_active_at` was stamped at the exact moment of the
    #: boundary that lost it. Recorded rather than resolved into every path,
    #: because a relative path stays readable to a person and the join is one
    #: step for whoever needs the absolute form.
    work_root: str = ""

    # ---- the effect half. Recovery's fields, never the boundary's. ----

    #: Which of the six this is. Defaulted so a row written before the axis
    #: existed loads as the only kind there was, which is what it is.
    boundary: str = CONSOLIDATION

    #: The run this belongs to, for a turn with no conversation behind it.
    #: Evidence on a chat-keyed row, and the FILE KEY on one without a chat.
    run_id: str = ""

    #: The call this boundary is about, on `before_tool` and `after_tool`.
    #: `call_id` is the model's own `tool_use` id, so the row points into the
    #: transcript rather than copying any of it.
    tool: str = ""
    call_id: str = ""

    #: What the tool declared about repetition when the call was made, read
    #: from `kernel/tools/recovery.py`. Recorded rather than resolved later:
    #: a tool can be edited, a custom one can be retired, and a recovery pass
    #: that re-asked the registry would be answering about today's tool rather
    #: than the one that ran.
    recovery: str = ""

    #: `{kind, id, locator}` from `kernel/tools/receipt.py`, or empty. A
    #: reference by construction, which is why the whole record can hold one
    #: without holding a copy of anything.
    receipt: dict[str, str] = field(default_factory=dict)

    def is_empty(self) -> bool:
        """Whether this says anything at all about the work.

        A boundary on a conversation that was never about a task produces one of
        these, and that is correct: the fact that nothing was owed is itself
        what a later reader needs. It is recorded, not dropped, so the absence
        of a checkpoint always means the boundary did not run.
        """
        return not any(
            (
                self.objective,
                self.phase,
                self.completed,
                self.remaining,
                self.next_action,
                self.open_questions,
                self.blocked_by,
                self.artifacts,
            )
        )


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:FIELD_CHARS]


def _clean_list(value: Any) -> list[str]:
    """A list of short strings, or nothing.

    A model handed a list field returns a string about as often as a list, and
    the useful reading of one string is a list of one rather than an empty
    field. Anything else in the list is dropped rather than coerced: `str()` on
    a dict produces something that looks like content and is not.
    """
    if isinstance(value, str):
        cleaned = _clean_text(value)
        return [cleaned] if cleaned else []
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        text = _clean_text(item)
        if text:
            out.append(text)
        if len(out) >= LIST_CAP:
            break
    return out


def build(
    *,
    session_id: str,
    chat_id: str = "",
    trigger: str,
    outcome: str,
    refused: str = "",
    state: dict[str, Any] | None,
    boundary: str = CONSOLIDATION,
    run_id: str = "",
    tool: str = "",
    call_id: str = "",
    recovery: str = "",
    receipt: dict[str, str] | None = None,
) -> Checkpoint:
    """Make a checkpoint out of whatever the boundary reported.

    `state` is the model's, so nothing in it is trusted for shape. Every field
    is cleaned to a bounded string or a bounded list of them, and a key that is
    missing, null, or the wrong type lands as empty. The runtime validates and
    never infers: a field the model left empty is empty.

    `boundary` is the RUNTIME's and raises when it is not one of the six. The
    caller catches everything here, so the cost of a typo would otherwise be a
    row on disk that neither reader can see, which is the one failure this
    store cannot report to anybody.
    """
    if boundary not in BOUNDARIES:
        raise ValueError(
            f"checkpoint boundary {boundary!r} is not one of {sorted(BOUNDARIES)}. "
            "The set is closed: a row neither reader knows about is worse than "
            "no row, because the file still looks written to."
        )
    src = state if isinstance(state, dict) else {}
    return Checkpoint(
        checkpoint_id=uuid.uuid4().hex,
        ts=datetime.now(timezone.utc).isoformat(),
        session_id=str(session_id or ""),
        chat_id=str(chat_id or ""),
        trigger=str(trigger or ""),
        outcome=str(outcome or ""),
        refused=_clean_text(refused),
        objective=_clean_text(src.get("objective")),
        phase=_clean_text(src.get("phase")),
        completed=_clean_list(src.get("completed")),
        remaining=_clean_list(src.get("remaining")),
        next_action=_clean_text(src.get("next_action")),
        open_questions=_clean_list(src.get("open_questions")),
        blocked_by=_clean_text(src.get("blocked_by")),
        artifacts=_clean_list(src.get("artifacts")),
        # Read here, not taken from `state`: the model is not asked for it and
        # cannot get it wrong. Best-effort — a conversation with no active
        # project records an empty root, which is the honest answer and the
        # same one the field had before it existed.
        #
        # Only on a consolidation, and that is a hot-path decision as much as
        # an ownership one. It opens the project registry off disk, and the
        # five other boundaries fire several times a turn: a cursor row would
        # pay a file read to record where artifacts hang off, when a cursor
        # row has no artifacts. The field describes the WORK, which is the
        # boundary's half of this record and not recovery's.
        work_root=_active_work_root() if boundary == CONSOLIDATION else "",
        boundary=boundary,
        run_id=str(run_id or ""),
        tool=_clean_text(tool),
        call_id=_clean_text(call_id),
        recovery=_clean_text(recovery),
        # Three known keys and nothing else, so a caller handing this the whole
        # tool result cannot turn a reference into a copy of one.
        receipt={
            k: _clean_text((receipt or {}).get(k))
            for k in ("kind", "id", "locator")
            if _clean_text((receipt or {}).get(k))
        },
    )


def _active_work_root() -> str:
    """The active project's root, or empty.

    Never raises into a boundary. The checkpoint is the only thing standing
    between one context and the next, so a registry that will not load costs a
    field here and never the record.
    """
    try:
        from tesseract.orchestrator.projects import ProjectStore

        active = ProjectStore().active()
        return str(getattr(active, "root", "") or "") if active else ""
    except Exception:
        log.debug("checkpoints: no active project root to record", exc_info=True)
        return ""
