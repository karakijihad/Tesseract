"""The turn manifest — one row per step of a conversation turn, on disk.

A background job has survived a hard kill since AR-2: its manifest is committed
after every stage, so a restart costs one stage instead of a night. A turn had
no equivalent. Its steps lived in an asyncio task and nowhere else, so a
backend that went down mid-turn left nothing for the next boot to find, nothing
for the person who was waiting to be told, and nothing anybody could read
afterwards to answer "did that work".

Measured on 2026-08-24: a channel turn opened at 22:09:14, the process took a
stop request at 22:09:21, the drain cancelled the turn at 22:09:31, and the
boot that followed reported seventeen preserved workers and no turns at all,
because there were no turn records to count.

**The record shape is AR-2's, deliberately.** `RunManifest` and `StageRow`
already carry a run id, an entry, one row per step in execution order, an
outcome from the closed vocabulary, a reason, counts and durations, and they
are already committed atomically. A second shape for the same claim is how two
surfaces come to disagree about what a run is.

**The store is not AR-2's, and cannot be.** `ManifestStore` keeps a single
`current.json` open slot, which is right for a nightly pipeline that runs once
at a time and wrong for turns: the cockpit and every channel can each be mid
turn simultaneously, and one open slot would have them overwrite each other.
So an open turn is its own file under `open/`, and closing one moves it into
the dated directory the retention sweep reads.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from tesseract.lib.clock import to_local
from tesseract.orchestrator.outcome import RunOutcome
from tesseract.paths import runtime_dir
from tesseract.scheduler.pipeline.artifacts import atomic_write_json
from tesseract.scheduler.pipeline.manifest import RunManifest, StageRow

log = logging.getLogger(__name__)

# A turn id is `<session id>.<random suffix>`, and the separator is load
# bearing: it is how a record found on disk at boot names the conversation
# whose person is waiting. Session ids are uuid hex or `telegram_<chat>_<hex>`
# and never contain a dot, so the LAST dot splits them unambiguously.
_ID_SEPARATOR = "."
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


#: Set once this process is on its way out, BEFORE anything starts dying.
#:
#: Measured 2026-08-31, nine milliseconds on the live machine: the stop
#: request raised SIGINT at .868, the turn's record closed itself at .871
#: saying `failed` / *the turn ended without recording how*, and the shutdown
#: intent was written at .879. Nothing in the process knew it was stopping
#: until after the turn had already filed its own account of why it stopped.
#:
#: So the record was wrong in the one way that mattered: it said the turn
#: ended for no reason anybody could name, and it was CLOSED, which put it out
#: of reach of the boot-side recovery that exists to tell the person. A
#: `threading.Event` because the stop watcher is a background thread and the
#: turn is on the event loop.
_going_down = threading.Event()


def note_going_down() -> None:
    """Say the process is stopping. Called before the signal, never after."""
    _going_down.set()


def going_down() -> bool:
    """Whether this process is stopping. Read by `TurnRecorder.close`."""
    return _going_down.is_set()


def forget_going_down() -> None:
    """For tests. A module-level flag outlives one test without this."""
    _going_down.clear()


def turns_root() -> Path:
    """`runtime/turns/` — open turns, and the dated directories they close into."""
    return runtime_dir() / "turns"


def turn_session_id(turn_id: str) -> str:
    """The session a turn id was minted for, or `""` if it carries none."""
    session, sep, _suffix = turn_id.rpartition(_ID_SEPARATOR)
    return session if sep else ""


def _mint(session_id: str) -> str:
    return f"{_UNSAFE.sub('-', session_id) or 'session'}{_ID_SEPARATOR}{secrets.token_hex(4)}"


#: What to call a turn on a surface, by the door it came through. The entry is
#: the manifest's own: `cockpit`, `terminal`, `schedule`, or `channel:<name>`.
_DOOR_LABELS = {
    "cockpit": "What you asked in the cockpit",
    "channel": "What you asked on a channel",
    "terminal": "What you asked in the terminal",
    "schedule": "A scheduled question",
}


def turn_label(entry: str) -> str:
    """What to call a turn on a surface, in the door's own words.

    Never the operator's own text: this names a row on a panel and a node on a
    map, and what they asked is between them and the reply. One function for
    the liveness row and the atlas node, so the two cannot name one turn two
    ways."""
    door, _, channel = (entry or "").partition(":")
    if door == "channel" and channel:
        return f"What you asked on {channel.capitalize()}"
    return _DOOR_LABELS.get(door, "What you asked")


def step_name(index: int, kind: str, name: str) -> str:
    """A step's `stage`, which carries its ordinal.

    A turn calls the same tool many times and `RunManifest.committed` is a set
    of stage names, so without the ordinal the second call collides with the
    first. It is also what a reader follows to see where the turn stopped."""
    return f"{index:03d}.{kind}.{name}"


def read_step_name(stage: str) -> tuple[str, str]:
    """The `(kind, name)` a step's `stage` was built from, or `("", "")`.

    Beside the builder deliberately: a second place that knows the format is a
    second place to fix when it changes."""
    parts = stage.split(".", 2)
    return (parts[1], parts[2]) if len(parts) == 3 else ("", "")


#: Kinds the RUNTIME writes about its own filing, as against work the turn did.
#: `told` and `end` both land after the last real step, so a reader that takes
#: the last row describes our bookkeeping instead of the person's question. A
#: bookkeeping kind added later belongs in here, or it starts appearing in copy.
_BOOKKEEPING_KINDS = frozenset({"turn"})


def what_it_reached(manifest: RunManifest) -> str:
    """Plain words for the last thing a turn actually did.

    Walked backwards rather than read off the end. `told` is committed after
    the work, so on 2026-09-01 this sentence reached the operator's phone
    reading *the last step was 001.turn.told* — the runtime describing its own
    filing, in a slug, to somebody asking about the weather.

    A stage name is never printed. It is an ordinal, a kind and a tool name
    joined by dots, and nobody reading a record of their own turn can resolve
    one.
    """
    for row in reversed(manifest.rows):
        kind, name = read_step_name(row.stage)
        if kind in _BOOKKEEPING_KINDS:
            continue
        if kind == "tool":
            return f"the last thing it did was run {name}"
        if kind == "model":
            return "it was waiting on a reply from the model"
        return "it was partway through a step"
    return "nothing had happened yet"


# What a shutdown says to somebody mid answer. Separate from the sentence
# below because the drain knows something that path does not: the reason is a
# restart, and "it will be back" is the remedy. Here rather than in the bridge
# so the words belong to the runtime, whichever door they leave through.
SHUTDOWN_NOTICE = (
    "The app is restarting and I was still working on that, so the answer "
    "never came. Send it again once it is back."
)


def why_there_was_no_reply(outcome: RunOutcome | None, reason: str = "") -> str:
    """What to tell the person when a turn ended and said nothing.

    One string covered three different failures: a cancellation, an empty
    stream, and every step gated all reached the operator as "(no reply
    produced this turn)", which says what did not happen and nothing about
    what did or what to do next.

    Here rather than in a transport, because both doors owe the same sentence.
    A surface may differ in what it can physically carry; it does not get to
    write its own account of why the runtime went quiet.

    **A shutdown answers here, and not with a second sentence of its own.** On
    2026-09-01 one interrupted turn reached the operator twice: the bubble they
    were watching said the turn was stopped, and the drain then sent
    `SHUTDOWN_NOTICE` as a fresh message saying the same event in different
    words. Two tellers is a transport problem; two ACCOUNTS is this function's,
    because it is the one that owns what a silent turn is called."""
    if outcome is RunOutcome.TRUNCATED:
        if going_down():
            return SHUTDOWN_NOTICE
        return (
            "I was still working on that when the turn was stopped, so the "
            "answer never got finished. Nothing picks it up on its own, so "
            "send it again if you still need it."
        )
    if outcome is RunOutcome.REFUSED:
        return reason.strip() or (
            "That turn was stopped before it began, so nothing ran. Try again."
        )
    if outcome is RunOutcome.FAILED:
        return (
            "That turn failed partway through and there is no answer to give "
            "you. Try again, and rephrase if it fails the same way."
        )
    return (
        "The turn finished without producing a reply. Nothing was said and "
        "nothing was left half done. Ask again, or rephrase."
    )


def was_told(manifest: RunManifest) -> bool:
    """Whether anybody reached the person waiting on this turn.

    False for a turn nothing tried to tell about, and false for one where the
    telling was attempted and failed, which are the same thing to the person
    and so are the same answer here."""
    for row in manifest.rows:
        if read_step_name(row.stage) == ("turn", TurnRecorder.TOLD):
            return row.outcome is RunOutcome.SUCCEEDED
    return False


def note_told(
    manifest: RunManifest,
    *,
    reached: bool,
    reason: str = "",
    store: TurnManifestStore | None = None,
) -> None:
    """Record on an OPEN record that the person was told, or that we tried.

    For the shutdown path, where the turn is already over and its recorder is
    gone but the record is still in `open/` waiting for the next boot. The row
    is the same one `TurnRecorder.told` writes, so a reader has one shape to
    know. Best effort: the sentence has already been sent, and failing to note
    it costs a duplicate at the next boot rather than a silence.
    """
    ended = datetime.now(timezone.utc)
    manifest.rows.append(
        StageRow(
            stage=step_name(len(manifest.rows), "turn", TurnRecorder.TOLD),
            outcome=RunOutcome.SUCCEEDED if reached else RunOutcome.FAILED,
            reason=reason,
            started_at=ended,
            ended_at=ended,
        )
    )
    try:
        (store or TurnManifestStore()).commit(manifest)
    except OSError:
        log.exception("turn manifest: could not note the telling on %s", manifest.run_id)


def close_interrupted(
    manifest: RunManifest, reason: str, *, store: TurnManifestStore | None = None
) -> Path:
    """Close a turn nobody is running any more, keeping every committed step.

    Recovery's path. The turn ended without anybody deciding it should, which
    is `truncated`, and the record says so in the same closing row a turn that
    ended normally writes. The row spans from the turn's start, so its duration
    is how long the record sat open rather than a step's own time."""
    ended = datetime.now(timezone.utc)
    manifest.rows.append(
        StageRow(
            stage=step_name(len(manifest.rows), "turn", "end"),
            outcome=RunOutcome.TRUNCATED,
            reason=reason,
            started_at=manifest.started_at,
            ended_at=ended,
            duration_ms=(ended - manifest.started_at).total_seconds() * 1000.0,
        )
    )
    return (store or TurnManifestStore()).finish(manifest)


class TurnManifestStore:
    """`runtime/turns/open/<turn id>.json` while a turn runs; a dated
    directory beside it once the turn ends."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or turns_root()

    @property
    def open_dir(self) -> Path:
        return self._root / "open"

    def open_path(self, turn_id: str) -> Path:
        return self.open_dir / f"{turn_id}.json"

    def closed_path(self, manifest: RunManifest) -> Path:
        # A day-named directory is a calendar day, so it is the operator's.
        # `playbook_extract._day` walks these names to find a task's records
        # and moved with it; the two are one choice, not two.
        day = to_local(manifest.started_at).date().isoformat()
        return self._root / day / f"{manifest.run_id}.json"

    def open_paths(self) -> list[Path]:
        """Every file under `open/`, readable or not.

        `load_open` skips what it cannot parse, which is right at boot and
        leaves the caller unable to say how many records it did not get. The
        difference between these two is that number."""
        return sorted(self.open_dir.glob("*.json")) if self.open_dir.is_dir() else []

    def load_open(self) -> list[RunManifest]:
        """Every turn that never finished, oldest first.

        A file that cannot be read is logged and skipped rather than raised on:
        this is read at boot, and one corrupt record must not cost the scan the
        other turns it was going to recover.
        """
        found: list[RunManifest] = []
        for path in self.open_paths():
            try:
                found.append(
                    RunManifest.from_dict(
                        json.loads(path.read_text(encoding="utf-8"))
                    )
                )
            except (OSError, ValueError, KeyError):
                log.exception("turn manifest: unreadable open turn at %s", path)
        found.sort(key=lambda m: m.started_at)
        return found

    def load_open_one(self, turn_id: str) -> RunManifest | None:
        """One open record by id, or `None` when there is nothing to read.

        Here rather than in the caller because knowing where an open record
        lives is this class's, and the caller that wants one is a transport
        marking a turn as told.
        """
        if not turn_id:
            return None
        try:
            body = self.open_path(turn_id).read_text(encoding="utf-8")
            return RunManifest.from_dict(json.loads(body))
        except (OSError, ValueError, KeyError):
            return None

    def commit(self, manifest: RunManifest) -> None:
        atomic_write_json(self.open_path(manifest.run_id), manifest.to_dict())

    def finish(self, manifest: RunManifest) -> Path:
        manifest.completed_at = datetime.now(timezone.utc)
        path = self.closed_path(manifest)
        atomic_write_json(path, manifest.to_dict())
        try:
            self.open_path(manifest.run_id).unlink(missing_ok=True)
        except OSError:
            log.exception(
                "turn manifest: could not clear %s", self.open_path(manifest.run_id)
            )
        return path


class TurnRecorder:
    """The live half: one per turn, holding the manifest and writing it out
    after every step.

    Every method swallows its own IO errors. A turn must not fail because its
    record could not be written, which is the same rule memory writes follow:
    the work is the point and the record is best effort.
    """

    def __init__(
        self,
        *,
        entry: str,
        session_id: str = "",
        store: TurnManifestStore | None = None,
        now: datetime | None = None,
    ) -> None:
        started = now or datetime.now(timezone.utc)
        self._store = store or TurnManifestStore()
        self.manifest = RunManifest(
            run_id=_mint(session_id),
            anchor=started,
            started_at=started,
            entry=entry,
        )
        self._commit()

    @property
    def turn_id(self) -> str:
        return self.manifest.run_id

    def bind_task(self, task_id: str) -> None:
        """Say which task this turn is working, and commit it at once, so a
        kill between now and the turn's end still leaves the join on disk."""
        self.manifest.task_id = task_id
        self._commit()

    def step(
        self,
        *,
        kind: str,
        name: str,
        outcome: RunOutcome,
        reason: str = "",
        started_at: datetime | None = None,
    ) -> None:
        """Record one step and commit. `kind` is `model` or `tool`.

        The row's `stage` carries its ordinal, because a turn calls the same
        tool many times and `RunManifest.committed` is a set of stage names. An
        ordinal makes each step its own row rather than a collision, and it is
        what a resume reads to find where the turn stopped.
        """
        ended = datetime.now(timezone.utc)
        began = started_at or ended
        self.manifest.rows.append(
            StageRow(
                stage=step_name(len(self.manifest.rows), kind, name),
                outcome=outcome,
                reason=reason,
                started_at=began,
                ended_at=ended,
                duration_ms=(ended - began).total_seconds() * 1000.0,
            )
        )
        self._commit()

    TOLD = "told"

    def told(self, *, reached: bool, reason: str = "") -> None:
        """Record that the person waiting was told the app is going down.

        A step and not a field, because the record already has a vocabulary for
        "something happened during this turn" and a second one would be a
        second shape for the same claim. `reached` false is the case that
        matters: the next boot reads it and does the telling the bridge could
        not, which on the run that found all this was a real possibility,
        because Telegram had refused a call 0.8 seconds earlier.
        """
        self.step(
            kind="turn",
            name=self.TOLD,
            outcome=RunOutcome.SUCCEEDED if reached else RunOutcome.FAILED,
            reason=reason,
        )

    def close(self, outcome: RunOutcome, reason: str = "") -> None:
        """End the turn with one outcome, and move the record out of `open/`.

        The closing outcome is a row like any other, so a reader that walks the
        rows in order sees how the turn ended without having to know that the
        last one is special.

        **Unless the process is going down, in which case the record stays
        open.** A turn cut short by a restart did not end, it was stopped, and
        the honest record of that is the one still sitting in `open/` for the
        next boot to find. Closing it here is what hid the 2026-08-31 failure:
        `send`'s own `finally` wrote `failed` / *the turn ended without
        recording how* seventeen milliseconds before the bridge was reached,
        so recovery had nothing left to recover and the person waiting was
        never told by anybody. The next boot closes it as `truncated`, with the
        sentence that says a restart took it.
        """
        if going_down():
            log.info(
                "turn manifest: %s left open — the app is stopping, so the "
                "next boot closes it and tells whoever was waiting",
                self.turn_id,
            )
            return
        self.step(kind="turn", name="end", outcome=outcome, reason=reason)
        try:
            self._store.finish(self.manifest)
        except OSError:
            log.exception("turn manifest: could not close %s", self.turn_id)

    def _commit(self) -> None:
        try:
            self._store.commit(self.manifest)
        except OSError:
            log.exception("turn manifest: could not commit %s", self.turn_id)


__all__ = [
    "SHUTDOWN_NOTICE",
    "TurnManifestStore",
    "TurnRecorder",
    "close_interrupted",
    "forget_going_down",
    "going_down",
    "note_going_down",
    "note_told",
    "read_step_name",
    "step_name",
    "turn_label",
    "turn_session_id",
    "turns_root",
    "was_told",
    "what_it_reached",
    "why_there_was_no_reply",
]
