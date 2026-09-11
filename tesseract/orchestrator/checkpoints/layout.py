"""Where a checkpoint lives, and every limit the store holds itself to.

Both halves in one file because they are one subject: a path decides which file
a row lands in, and these numbers decide how large that file may get and how
much of it a reader takes. The writer and the readers both need them, and a
limit defined in whichever of the two happened to want it first is how two
answers to one policy start.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

from tesseract.paths import TESSERACT_HOME


def checkpoint_dir() -> Path:
    """Resolve ``<TESSERACT_HOME>/checkpoints/`` at call time."""
    override = os.environ.get("TESSERACT_HOME")
    home = Path(override).resolve() if override else TESSERACT_HOME
    return home / "checkpoints"


#: Where a checkpoint goes when the conversation has no durable id, which is a
#: sub-agent or a synthetic turn. A leading underscore cannot collide with a
#: chat id: those are hex, and `_safe_name` only ever emits characters that
#: were already there or a dash.
UNKEYED = "_unkeyed"


def _safe_name(chat_id: str) -> str:
    """A chat id as a filename, without inventing a second identity for it.

    Every id the runtime stamps is already a hex record id, so in practice
    this changes nothing. It exists because this function turns a value into a
    PATH, and a value that reaches a path unchecked is how a chat id with a
    slash in it writes somewhere nobody meant.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", chat_id).strip(".-")[:120]
    return cleaned or UNKEYED


def checkpoint_path(chat_id: str = "", *, run_id: str = "") -> Path:
    """This conversation's own append-only file, or this run's.

    The chat wins when there is one: a task worked inside a conversation
    belongs to that conversation, and a second file for the same work would be
    the drift this store's whole layout is chosen to avoid. `run-` prefixes
    the other kind so the two namespaces cannot land on one name, however the
    ids are minted.
    """
    if chat_id:
        return checkpoint_dir() / f"{_safe_name(chat_id)}.jsonl"
    if run_id:
        return checkpoint_dir() / f"run-{_safe_name(run_id)}.jsonl"
    return checkpoint_dir() / f"{UNKEYED}.jsonl"


#: When a file is pruned. Two properties buy this number, not one.
#:
#: Growth: `retention/sweeps.py` ages a checkpoint file on its NEWEST row, so a
#: conversation used every day is never old enough for the 30-day window to
#: reach it. That was fine while a chat wrote one row per boundary. It writes
#: several per turn now, so without a ceiling the file for the daily project
#: chat grows for as long as that chat exists.
#:
#: Read cost: `latest_for_chat` wants consolidations, and reaching one means
#: walking back past every cursor row above it. At this size the worst walk is
#: a few hundred rows, which is the difference between a couple of milliseconds
#: and a stall on the loop that health, heartbeats and every other conversation
#: share.
MAX_BYTES = 256 * 1024


#: How many cursor rows survive a prune. Recovery reads the NEWEST one; the
#: ones under it are there so a person reading the file can see how the turn
#: got where it did. Consolidations are never pruned: they are the record, and
#: they are sparse enough to keep.
KEEP_STEPS = 40


#: The row limit that means "the whole file". Named once and shared by every
#: whole-file reader, because a number written out three times is three
#: policies waiting to disagree: the boot pass and the brief a resumed turn
#: acts on have to see the same wreckage, and they read through different
#: functions. The real ceiling is `MAX_BYTES`, which the prune enforces; this
#: is only high enough never to be the thing that stops a read.
WHOLE_FILE = 100_000


#: What the paired call readers answer with when nobody names a window. They
#: are two halves of one question, "what happened to this call", so one number
#: decides both: a window that answered about opens and closes differently
#: would report a call as open because its close fell off the end.
RECENT_CALLS = 200


#: How much of a file's end one read looks at before asking for more. A row is
#: a few hundred bytes, so this is dozens of them: enough that the answer is
#: almost always one read, small enough that it is never a whole history.
TAIL_BYTES = 64 * 1024


#: Appends and prunes are both writes to one file from `asyncio.to_thread`, so
#: two of them are two THREADS. Without this a prune could rewrite the file
#: between another thread opening it and appending, and the appended row would
#: land in a file that no longer exists under that name.
WRITE_LOCK = threading.Lock()


#: Written once the per-day files have been folded in, so the fold is asked
#: about once per home rather than on every read.
MIGRATED_MARK = ".per-chat"


#: A per-day file, which is what this store wrote before the layout changed.
DAILY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
