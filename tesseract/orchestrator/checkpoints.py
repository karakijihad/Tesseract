"""What the boundary wrote down: the working state a fresh context is rebuilt from.

A consolidation reflects and then either continues or stops. Reflection already
writes what the conversation taught, as deltas into the memory store. What it
never wrote is what the conversation was DOING, and without that a continue has
nothing to rebuild from and a stop leaves nothing to come back to.

So every boundary writes one checkpoint. Both outcomes: a reset writes one too,
because the memory writer needs no separate logic for the two and only the
orchestration afterwards differs.

**References, never copies.** A transcript, a prompt, a tool result and a file
each already have an owner. A checkpoint holding a copy of one is a second truth
that drifts from the first, and the drift is invisible because both look
authoritative. `artifacts` holds paths and ids; nothing here holds a body.

**Per-day JSONL, resolved at call time.** The same shape as
`orchestrator/autonomy/journal.py`, for the same reason: a test pointing
`TESSERACT_HOME` somewhere else is answered by that home without re-importing
the module. Append-only, and a read never blocks a write.

**Not a workspace event.** Those are reserved for threads the operator is
expected to answer, and a checkpoint is runtime bookkeeping nobody is being
asked about. Putting it in the inbox would cost the operator attention on every
boundary the runtime crossed by itself.

What this file does NOT own, and must not grow: `recovery_behaviour`, receipts,
and whether repeating an external effect is safe. Those belong to the recovery
work and are written onto the same record when it lands.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tesseract.paths import TESSERACT_HOME

log = logging.getLogger(__name__)

#: How many of each list a checkpoint keeps. A boundary that reports two hundred
#: remaining items is reporting a plan, not a state, and the tail CC-8 builds
#: from this has to fit in a context window.
LIST_CAP = 20

#: Per string. Long enough for a sentence a person reads, short enough that a
#: model cannot smuggle a transcript into a field meant for a reference.
FIELD_CHARS = 600


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


def checkpoint_dir() -> Path:
    """Resolve ``<TESSERACT_HOME>/checkpoints/`` at call time."""
    override = os.environ.get("TESSERACT_HOME")
    home = Path(override).resolve() if override else TESSERACT_HOME
    return home / "checkpoints"


def checkpoint_path(day: datetime | None = None) -> Path:
    """Per-day JSONL path. ``day`` defaults to now, in UTC."""
    when = day or datetime.now(timezone.utc)
    return checkpoint_dir() / f"{when.strftime('%Y-%m-%d')}.jsonl"


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
) -> Checkpoint:
    """Make a checkpoint out of whatever the boundary reported.

    `state` is the model's, so nothing in it is trusted for shape. Every field
    is cleaned to a bounded string or a bounded list of them, and a key that is
    missing, null, or the wrong type lands as empty. The runtime validates and
    never infers: a field the model left empty is empty.
    """
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
    )


def write(checkpoint: Checkpoint) -> str | None:
    """Append one checkpoint. Returns its id, or `None` if it could not be written.

    Best-effort, like the operator journal it is shaped after. The boundary has
    already reflected and is about to fold or end a conversation by the time
    this runs, and a disk that will not take the note must not turn a completed
    boundary into a failed turn. The caller gets `None` and can say so; nothing
    raises out of here.
    """
    path = checkpoint_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(asdict(checkpoint), default=str) + "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        log.exception("checkpoints: append failed for session=%s", checkpoint.session_id)
        return None
    return checkpoint.checkpoint_id


def read_recent(limit: int = 20, *, days: int = 7) -> list[Checkpoint]:
    """The most recent checkpoints, newest first.

    Malformed lines are skipped and logged rather than raising: the file is
    append-only and a truncated last line is what a killed process leaves
    behind, which must not make every earlier checkpoint unreadable.
    """
    out: list[Checkpoint] = []
    root = checkpoint_dir()
    if not root.exists():
        return out
    for path in sorted(root.glob("*.jsonl"), reverse=True)[:days]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            log.exception("checkpoints: could not read %s", path)
            continue
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                out.append(Checkpoint(**json.loads(line)))
            except (ValueError, TypeError):
                log.warning("checkpoints: skipping a line that will not load in %s", path)
            if len(out) >= limit:
                return out
    return out


def recent_for_chat(chat_id: str, *, limit: int = 5, days: int = 7) -> list[Checkpoint]:
    """This conversation's own boundaries, newest first.

    What "continuing needs a reason" is decided from: one checkpoint says what
    a boundary reported, and a few in a row say whether anything is actually
    moving.
    """
    if not chat_id:
        return []
    out: list[Checkpoint] = []
    for checkpoint in read_recent(limit=1000, days=days):
        if checkpoint.chat_id != chat_id:
            continue
        out.append(checkpoint)
        if len(out) >= limit:
            break
    return out


def latest_for_chat(chat_id: str, *, days: int = 7) -> Checkpoint | None:
    """The newest checkpoint this conversation wrote, if it wrote one.

    What the continuity tail is rebuilt from. Scans rather than indexes: a week
    of boundaries is a small file, and an index is a second truth to keep in
    step with the first.

    Keyed on the conversation and not on the connection. It was written the
    other way and was wrong on the surface it was written for: every cockpit
    chat open on one WebSocket shares `session_id`, so a fresh chat asking what
    it was doing was handed whichever chat on that connection had reflected
    most recently, and a page reload made every earlier boundary unfindable.
    """
    found = recent_for_chat(chat_id, limit=1, days=days)
    return found[0] if found else None


__all__ = [
    "Checkpoint",
    "LIST_CAP",
    "FIELD_CHARS",
    "build",
    "write",
    "read_recent",
    "latest_for_chat",
    "recent_for_chat",
    "checkpoint_dir",
    "checkpoint_path",
]
