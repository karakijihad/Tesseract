"""What a resumed turn is told, and the one place that answers it.

`effects.py` decides. This hands the decision to whoever picks the work back
up, and it is deliberately one reader rather than one per door. Three things
resume work in this runtime: a task taken up out of `resume_queued`, a
conversation the runtime wakes because the last process died inside it, and
the operator simply carrying on in the chat. A digest shaped for any one of
them is a second answer to "what was I doing", and the two would drift the
first time a boundary was added.

**It reads and never writes.** Nothing here retries a call, closes a row or
touches a task. The whole of the acting half is that a turn is TOLD, in words
it can act on, which calls are verified and which are open, and what the tool
itself said about repeating each. Doing it for the turn would mean the runtime
re-issuing a call whose arguments only the transcript holds, and guessing
those is the one failure this phase exists to prevent.

**Verified means a receipt, not a close.** A call that ended is not evidence
that anything landed; a call that ended carrying an identifier the far side
minted is. So the verified list is the closed rows whose receipt points at
something, and a close with `Receipt.nothing` on it is left out rather than
counted as proof of an effect that never existed.

**The arguments live in the transcript and stay there.** A checkpoint holds
references and no copies, so the brief names the call id rather than the
call. That is what "run it again with the same key" means here: the resumed
turn finds its own `tool_use` block above and repeats it. A turn that cannot
find it says so and asks, because a reconstructed argument list is a new call
wearing an old call's name.

Nothing here relaxes a permission. A repeated call goes through
`permissions.yaml` and the bash-security checks exactly as a fresh one does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from typing import Collection

from tesseract.orchestrator import checkpoints
from tesseract.orchestrator.checkpoints import (
    WHOLE_FILE,
    Checkpoint,
    closed_calls,
    open_calls,
    row_ts,
)
from tesseract.orchestrator.recovery.effects import describe, when_of

log = logging.getLogger(__name__)

#: How many rows of each half reach a prompt. A brief that lists forty calls is
#: a transcript, and the turn reading it has a context window to spend on the
#: work rather than on its own history. The newest are the ones that matter:
#: an older open call is either already answered or is the operator's card.
VERIFIED_SHOWN = 5
OPEN_SHOWN = 10

#: What the turn may do about each call, keyed on what the tool declared. The
#: MEANING of each value is `effects.describe`'s, so the card the operator
#: reads and the brief the turn reads say the same thing about the same call;
#: only the instruction differs, because only one of them is being asked to
#: act.
INSTRUCTION: dict[str, str] = {
    "idempotent": (
        "Run it again with the same arguments. Its call id is {call_id}, so "
        "the arguments are in this conversation above. If you cannot find "
        "them, say so and ask rather than working them out again."
    ),
    "queryable": (
        "Check first. Ask the far side what happened, and repeat the call "
        "only if the check says it never landed. Its call id is {call_id}, "
        "so the arguments are in this conversation above."
    ),
    "unsafe": (
        "Leave it alone. Its call id is {call_id}. The operator has been asked "
        "about this one and the answer is theirs. Do not repeat it and do not "
        "undo it."
    ),
}


#: The conversations THIS boot found the last process had died inside, and the
#: RUNS inside each one that it died in. Filled by the effects scan and emptied
#: by whoever takes each one up.
#:
#: **The runs are carried, not just the chat, and that is what makes the bound
#: real.** The scan already refuses to offer a resumed turn over anything but a
#: run this crash interrupted; handing the waker a bare chat id threw that away
#: at the last step, because a chat read whole includes every call it ever left
#: open. The one most likely to be misread is exactly the oldest: a call from
#: weeks ago, outside what the turn can still see in its own transcript, where
#: "run it again with the same arguments" is an invitation to invent them.
#:
#: **Process state on purpose, and not a store.** It describes one boot's
#: wreckage and is meaningless to the next process, which will do its own scan
#: over the same rows and reach its own answer. A file here would be a second
#: record of something the checkpoint store already holds, kept only so that a
#: question could be asked twice.
#:
#: Written once, during the boot scans, and read from the event loop. Claiming
#: takes the whole entry, so two readers racing for one chat cannot both win.
_OWED: dict[str, set[str]] = {}


def note_owed(chat_id: str, run_id: str) -> None:
    """This conversation was left mid-call in this run, and has not been told.

    Both or neither. A blank run is not a narrower answer than no run at all,
    it is the SAME answer wearing a bound: `_runs_of` drops it, `read` then
    sees nothing to filter on and answers about the whole conversation, and
    the crash bound is gone with nothing saying so. So a row that cannot name
    its run is not owed a resumed turn; it is still counted, still carded when
    it is unsafe, and still there for the operator to ask about.
    """
    if chat_id and run_id:
        _OWED.setdefault(chat_id, set()).add(run_id)


def owed() -> frozenset[str]:
    """Every conversation still waiting to be told, for a caller that surveys."""
    return frozenset(_OWED)


def claim(chat_id: str) -> frozenset[str]:
    """Take the telling of one conversation, and the runs it is about.

    Empty for every caller but the first. A chat can be restored into two
    windows at once and each restore asks; the brief is the same both times,
    and saying it twice is the runtime telling somebody the same thing again,
    which is how a note stops being read.
    """
    return frozenset(_OWED.pop(chat_id, ()))


def forget_all() -> None:
    """Start from nothing, the way a boot does.

    Called by the recovery manager at the top of a pass as well as by tests: a
    second pass over the same state has to reach the same answer, and a set
    that only ever grows would re-offer a conversation somebody was already
    told about.
    """
    _OWED.clear()


@dataclass(frozen=True)
class Resumption:
    """What one conversation or run was standing in when the machine stopped."""

    chat_id: str = ""
    #: The runs this answer was narrowed to, or empty for the whole chat.
    runs: frozenset[str] = frozenset()
    verified: list[Checkpoint] = field(default_factory=list)
    in_the_air: list[Checkpoint] = field(default_factory=list)

    @property
    def anything_owed(self) -> bool:
        """Whether a turn has something to act on. Open calls, and only those.

        A conversation with receipts and nothing open finished what it was
        doing, and telling it about its own successful calls would be the
        runtime narrating history back at it.
        """
        return bool(self.in_the_air)


def read(chat_id: str = "", *, runs: str | Collection[str] = "") -> Resumption:
    """What this conversation or run left behind, off the checkpoint store.

    **`runs` is the only narrowing, and the narrowest thing you name is what
    you get.** A chat alone answers about the whole conversation, which is what
    the operator carrying on in it wants: any call it left open is unfinished
    business, whichever turn made it. A chat plus runs answers about those runs
    inside it, which is what a woken chat and a resumed task want, because both
    are about one crash and not about everything that ever went unrecorded
    here. Runs with no chat are scheduled turns and sub-agents, and each of
    those keys its own file.

    The narrowing has to be explicit. `checkpoint_path` lets the chat win when
    both are given, so passing a run to the store alone hands back the whole
    conversation, and a caller asking about one turn would act on somebody
    else's open call with nothing saying so.

    Never raises. A store that will not read costs the brief and never the
    turn, which is the rule every write in that store already follows: the
    work is the point and the record is best effort.
    """
    wanted = _runs_of(runs)
    try:
        if chat_id:
            opened = open_calls(chat_id, limit=WHOLE_FILE)
            closed = closed_calls(chat_id, limit=WHOLE_FILE)
            if wanted:
                opened = [r for r in opened if r.run_id in wanted]
                closed = [r for r in closed if r.run_id in wanted]
        else:
            # One file per run, so several runs is several reads. Merged on the
            # store's own idea of when a row happened rather than on the order
            # the files came back in, which is alphabetical and means nothing.
            opened, closed = [], []
            for run in sorted(wanted):
                opened.extend(open_calls("", run_id=run, limit=WHOLE_FILE))
                closed.extend(closed_calls("", run_id=run, limit=WHOLE_FILE))
            opened.sort(key=row_ts, reverse=True)
            closed.sort(key=row_ts, reverse=True)
    except Exception:  # noqa: BLE001 - a brief never fails a turn
        log.exception("resume: could not read the record for %s", chat_id or runs)
        return Resumption(chat_id=chat_id, runs=wanted)
    return Resumption(
        chat_id=chat_id,
        runs=wanted,
        # `.get("kind", "")` and not `.get("kind")`: `build` drops empty values
        # from the receipt, so a call that returned no receipt at all arrives
        # here as `{}`. Reading that as `None` put it in the verified list, and
        # it rendered as "doe_tool at 09:00 left ." with nothing after it: a
        # call presented as proof that something landed, on no evidence
        # whatever, which is the one thing this list must never contain.
        verified=[r for r in closed if r.receipt.get("kind", "") not in ("", "none")],
        in_the_air=opened,
    )


def _runs_of(runs: str | Collection[str]) -> frozenset[str]:
    """One run named, several named, or none. Empty strings are none."""
    if isinstance(runs, str):
        return frozenset({runs}) if runs else frozenset()
    return frozenset(r for r in runs if r)


def for_task(*, current_checkpoint: str | None, last_turn_id: str = "") -> str:
    """The brief for a task being taken back up, or empty when there is none.

    Takes the two references off the record rather than the record itself, so
    this package still knows nothing about the agenda. `current_checkpoint` is
    the anchor recovery wrote at the park; the last turn id is the fallback for
    a task parked before that field was written, and for one whose anchor row
    has since been pruned away.

    Empty is the honest answer when neither resolves, and the caller says the
    careful thing instead. A brief assembled from a task with no record behind
    it would be the runtime inventing what happened.
    """
    row = checkpoints.find(current_checkpoint or "")
    if row is None:
        row = checkpoints.latest_for_run(last_turn_id)
    if row is None:
        return ""
    return brief(read(row.chat_id, runs=row.run_id))


def brief(resumption: Resumption) -> str:
    """The whole of it in words, or empty when nothing is owed.

    Empty rather than a sentence saying nothing happened, because every caller
    uses this as the test: a body with no content in it is a turn nobody
    needed to take.
    """
    if not resumption.anything_owed:
        return ""
    lines = [
        "[resuming] The app stopped in the middle of this work and has "
        "started again. Nothing has been repeated for you.",
        "",
        _verified_block(resumption),
        "",
        _next_block(resumption),
        "",
        "A call you make now is checked for permission exactly as a fresh one "
        "is, and nothing here lets you skip that.",
    ]
    return "\n".join(lines)


def _verified_block(resumption: Resumption) -> str:
    if not resumption.verified:
        return (
            "What is verified: nothing. No call in this work finished with an "
            "identifier on it, so there is no evidence here that anything "
            "landed."
        )
    shown = resumption.verified[:VERIFIED_SHOWN]
    rows = [f"  {_verified_line(row)}" for row in shown]
    more = len(resumption.verified) - len(shown)
    if more > 0:
        rows.append(f"  and {more} more before those.")
    return "\n".join(["What is verified. These finished and left a mark:", *rows])


def _verified_line(row: Checkpoint) -> str:
    receipt = row.receipt
    what = " ".join(p for p in (receipt.get("kind"), receipt.get("id")) if p)
    where = receipt.get("locator") or ""
    tail = f" in {where}" if where else ""
    return f"{row.tool} at {when_of(row)} left {what}{tail}."


def _next_block(resumption: Resumption) -> str:
    shown = resumption.in_the_air[:OPEN_SHOWN]
    rows: list[str] = []
    for row in shown:
        instruction = INSTRUCTION.get(row.recovery, INSTRUCTION["unsafe"])
        rows.append(f"  {describe(row, name_the_place=False)}")
        rows.append(f"    {instruction.format(call_id=row.call_id or 'not recorded')}")
    more = len(resumption.in_the_air) - len(shown)
    if more > 0:
        rows.append(f"  and {more} more, older than those.")
    return "\n".join(
        [
            "What is next. Nothing recorded how these ended, so each one is "
            "decided by what the tool itself said about repeating it:",
            *rows,
        ]
    )


__all__ = [
    "INSTRUCTION",
    "OPEN_SHOWN",
    "VERIFIED_SHOWN",
    "Resumption",
    "brief",
    "claim",
    "for_task",
    "forget_all",
    "note_owed",
    "owed",
    "read",
]
