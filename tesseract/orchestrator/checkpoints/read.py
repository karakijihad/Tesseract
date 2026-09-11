"""Reading one file backwards, and the questions that answer from one.

Every reader here is told which file to open, by conversation or by run. The
ones that have to look across files are `scan.py`, and the split is not
cosmetic: these run several times a turn on the loop every conversation shares,
while those run at a boot or when a task is taken back up.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from tesseract.orchestrator.checkpoints import layout
from tesseract.orchestrator.checkpoints.layout import (
    RECENT_CALLS,
    WRITE_LOCK,
    checkpoint_path,
)
from tesseract.orchestrator.checkpoints.migrate import fold_daily_files
from tesseract.orchestrator.checkpoints.record import CONSOLIDATION, Checkpoint

log = logging.getLogger(__name__)


def tail(path: Path, *, want: int, boundary: str = "") -> list[Checkpoint]:
    """The newest `want` rows, read backwards from the end of the file.

    Reading the whole file was right while a conversation wrote one row per
    boundary and the file was a handful of lines. It writes several per turn
    now, so a long-lived conversation's file grows without limit while both
    readers are called on every turn, and a full parse of it would end up as
    tens of milliseconds of blocking work on the loop that health, heartbeats
    and every other conversation share.

    Backwards is safe because physical order IS chronological here: nothing
    ever rewrites this file except `fold_daily_files`, which sorts by
    timestamp on the way. A malformed line is skipped and logged,
    never raised, and the leading partial line of a chunk is never parsed until
    the read before it has completed it.
    """
    out: list[Checkpoint] = []
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            head = b""
            while end > 0 and len(out) < want:
                start = max(0, end - layout.TAIL_BYTES)
                fh.seek(start)
                block = fh.read(end - start) + head
                lines = block.split(b"\n")
                # The first element is a partial row unless this read reached
                # the start of the file. Carried into the next read rather
                # than parsed, which is how a row spanning a chunk edge
                # survives.
                head = lines.pop(0) if start > 0 else b""
                for raw in reversed(lines):
                    if not raw.strip():
                        continue
                    try:
                        row = Checkpoint(**json.loads(raw.decode("utf-8")))
                    except (ValueError, TypeError, UnicodeDecodeError):
                        log.warning(
                            "checkpoints: skipping a line that will not load in %s", path
                        )
                        continue
                    if boundary and row.boundary != boundary:
                        continue
                    out.append(row)
                    if len(out) >= want:
                        break
                end = start
    except OSError:
        log.exception("checkpoints: could not read %s", path)
    return out


def recent_for_chat(chat_id: str, *, limit: int = 5) -> list[Checkpoint]:
    """This conversation's own boundaries, newest first.

    What "continuing needs a reason" is decided from: one checkpoint says what
    a boundary reported, and a few in a row say whether anything is actually
    moving.

    No window and no cap. They were a `days=7` and a `limit=1000` over a shared
    per-day file, and both could hide a conversation's own rows from it without
    saying anything. The file is this conversation's, so its tail IS the
    answer.

    **Consolidations only.** The file now also carries the cursor rows recovery
    writes, several per turn, and answering with those would mean a caller
    asking "what did this conversation decide at its last boundary" is handed a
    row nobody reflected at. Every row written before the boundary axis existed
    is a consolidation, so this returns exactly what it always did.
    """
    return recent(chat_id, "", limit=limit, boundary=CONSOLIDATION)


def latest_for_chat(chat_id: str) -> Checkpoint | None:
    """The newest checkpoint this conversation wrote, if it wrote one.

    What the continuity tail is rebuilt from.

    Keyed on the conversation and not on the connection. It was written the
    other way and was wrong on the surface it was written for: every cockpit
    chat open on one WebSocket shares `session_id`, so a fresh chat asking what
    it was doing was handed whichever chat on that connection had reflected
    most recently, and a page reload made every earlier boundary unfindable.
    """
    found = recent_for_chat(chat_id, limit=1)
    return found[0] if found else None


def recent(
    chat_id: str,
    run_id: str,
    *,
    limit: int,
    boundary: str = "",
) -> list[Checkpoint]:
    """The tail of one file, newest first, optionally of one boundary kind."""
    if not chat_id and not run_id:
        return []
    with WRITE_LOCK:
        fold_daily_files()
    path = checkpoint_path(chat_id, run_id=run_id)
    if not path.is_file() or limit <= 0:
        return []
    return tail(path, want=limit, boundary=boundary)


def row_ts(row: Checkpoint) -> datetime:
    """A row's own time, and the far past when it has none.

    Never raises: a row whose stamp will not parse is one this pass cannot
    date, and treating it as ancient reports it rather than hiding it. The
    cost of being wrong is one question the operator did not need; the cost
    the other way is an effect nobody accounts for.
    """
    try:
        parsed = datetime.fromisoformat(row.ts)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_rows(raw_rows: list[str]) -> list[Checkpoint]:
    """Rows off disk as records, oldest first, skipping what will not load."""
    out: list[Checkpoint] = []
    for raw in raw_rows:
        if not raw.strip():
            continue
        try:
            out.append(Checkpoint(**json.loads(raw)))
        except (ValueError, TypeError):
            continue
    return out


def unclosed_in(rows: list[Checkpoint]) -> list[Checkpoint]:
    """The opens in one file with no close beside them, newest first.

    **One row per CALL, never per open boundary.** A call that parked on an
    approval has two of them, `before_tool` and `awaiting_operator`, and
    reporting both counts one uncertain effect twice everywhere it is read: on
    the recovery summary, in the sentence that tells the operator how many
    actions may or may not have finished, and in the brief a resumed turn acts
    on. The card deduplicates on the call id and would have been the only
    reader that got it right.

    The newest is the one kept, because it is the call's actual state and it is
    the more informative of the two: "it had stopped to ask you" says something
    "it was about to run" does not.

    `rows` must be newest first, which every caller passes: `tail` reads
    backwards, and `_prune` reverses its own parse before calling here.
    """
    closed = {r.call_id for r in rows if r.boundary == "after_tool" and r.call_id}
    seen: set[str] = set()
    out: list[Checkpoint] = []
    for row in rows:
        if row.boundary not in ("before_tool", "awaiting_operator") or not row.call_id:
            continue
        if row.call_id in closed or row.call_id in seen:
            continue
        seen.add(row.call_id)
        out.append(row)
    return out


def open_calls(chat_id: str = "", *, run_id: str = "", limit: int = RECENT_CALLS) -> list[Checkpoint]:
    """The calls this conversation or run opened and never closed, newest first.

    **This, not `latest_step`, is what a recovery pass reads.** A turn runs its
    concurrency-safe calls at the same time (`chat.py::_run_pending_calls`
    starts a task each), and several of those act: `api_request` is
    concurrency-safe and `unsafe`. So the newest PHYSICAL row is whichever call
    happened to finish last, and a call still in flight beside it is invisible
    behind that row. Reading the tail would report a turn as cleanly resolved
    while an unsafe effect was still unaccounted for, which is the one mistake
    this record exists to prevent.

    Pairing is on `call_id`, which is the model's own `tool_use` id in a turn
    and a minted one where there is no model. A row with no id cannot be paired
    and is never reported open, because a boundary nobody can match would be an
    alarm nothing could ever clear.
    """
    return unclosed_in(recent(chat_id, run_id, limit=limit))


def closed_calls(chat_id: str = "", *, run_id: str = "", limit: int = RECENT_CALLS) -> list[Checkpoint]:
    """The calls this conversation or run finished, newest first.

    `open_calls`'s other half, and the reason it is here rather than in the
    reader that wants it: a resumed turn is told what is VERIFIED before it is
    told what is open, and "verified" is exactly the rows that carry a receipt
    the far side minted. Answering that from anywhere but this store would
    mean a second reading of what a closed call looks like, and the two
    definitions would drift the first time a boundary was added.

    The receipt itself is left on the row rather than filtered for here. A
    close with no receipt is a real answer (`Receipt.nothing`), and which of
    the two a caller wants is the caller's question.
    """
    return [r for r in recent(chat_id, run_id, limit=limit) if r.boundary == "after_tool"]


def latest_step(chat_id: str = "", *, run_id: str = "") -> Checkpoint | None:
    """The last thing this conversation or run did, whatever kind it was.

    Where recovery resumes, and deliberately not `latest_for_chat`: the row
    that matters after a crash is the newest one, which is almost never a
    consolidation. A conversation that folded and then made three calls has a
    tail saying "reflected" and a truth saying "half-way through a tool".
    """
    return next(iter(recent(chat_id, run_id, limit=1)), None)
