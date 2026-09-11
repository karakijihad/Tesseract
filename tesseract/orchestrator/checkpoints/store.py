"""Putting a row on disk, and keeping the file it lands in bounded.

Append, prune, and the boundary writer the hot path calls. Nothing here reads a
record back for anybody else: the two directions are apart because a write here
is best effort by contract and a read is not, and one file holding both is how
a reader picks up a writer's habit of swallowing its own failures.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict
from pathlib import Path

from tesseract.orchestrator.checkpoints import layout
from tesseract.orchestrator.checkpoints.layout import (
    WRITE_LOCK,
    checkpoint_path,
)
from tesseract.orchestrator.checkpoints.migrate import fold_daily_files
from tesseract.orchestrator.checkpoints.read import parse_rows, unclosed_in
from tesseract.orchestrator.checkpoints.record import CONSOLIDATION, Checkpoint, build

log = logging.getLogger(__name__)


def write(checkpoint: Checkpoint) -> str | None:
    """Append one checkpoint. Returns its id, or `None` if it could not be written.

    Best-effort, like the operator journal it is shaped after. The boundary has
    already reflected and is about to clear a conversation by the time this
    runs, and a disk that will not take the note must not turn a completed
    boundary into a failed turn. The caller gets `None` and can say so; nothing
    raises out of here.
    """
    path = checkpoint_path(checkpoint.chat_id, run_id=checkpoint.run_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Before the append, never after: the fold puts the old rows at the
        # end of the file, and the end of the file is what `latest_for_chat`
        # reads.
        line = json.dumps(asdict(checkpoint), default=str) + "\n"
        with WRITE_LOCK:
            # Inside the lock, because the fold REWRITES these files the
            # same way the prune does, and two threads folding one target
            # is the race the prune took this lock to avoid.
            fold_daily_files()
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
            _prune(path)
    except OSError:
        log.exception("checkpoints: append failed for session=%s", checkpoint.session_id)
        return None
    return checkpoint.checkpoint_id


def _prune(path: Path) -> None:
    """Drop the cursor rows nobody will read again, keeping every boundary.

    Called under `WRITE_LOCK`, holding it across the read and the replace, so
    an append cannot land in the copy being retired.

    **A cursor row's whole life is the turn it describes.** Recovery reads the
    newest one; the ones under it are context for a person. A consolidation is
    the record itself and is never dropped here, which is why this can be a
    prune rather than a rotation: nothing that anything reads later is thrown
    away, so there is no second file to look in and no window in which a
    conversation cannot find its own last boundary.

    Best-effort, exactly like the append above it. A prune that fails leaves a
    file that is too big, which is a cost; a prune that raised would turn a
    written boundary into a failed one, which is the thing this store refuses
    to do.
    """
    try:
        if path.stat().st_size <= layout.MAX_BYTES:
            return
        rows = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        log.exception("checkpoints: could not read %s to prune it", path)
        return

    # An open call has to survive its own file. `_prune` was written when the
    # only reader was `open_calls`, which is asked about one conversation
    # right after that conversation crashed, before forty more rows could
    # land. `unresolved` reads at BOOT, over every file, about calls that
    # may have been left hanging while the turn carried on: dropping an
    # unclosed row would delete the only evidence that an effect is
    # unaccounted for, which is the one thing this record exists to keep.
    still_open = {r.call_id for r in unclosed_in(list(reversed(parse_rows(rows))))}

    kept: list[str] = []
    steps = 0
    # Backwards, so "the newest forty" is decided before anything is dropped.
    for raw in reversed(rows):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
            boundary = str(row.get("boundary") or CONSOLIDATION)
        except ValueError:
            # A line nothing can read is carried, for the reason the fold
            # carries one: deleting the only copy of a record to tidy up is
            # the wrong way round.
            kept.append(raw)
            continue
        if boundary == CONSOLIDATION:
            kept.append(raw)
            continue
        if str(row.get("call_id") or "") in still_open:
            kept.append(raw)
            continue
        if steps < layout.KEEP_STEPS:
            kept.append(raw)
            steps += 1
    kept.reverse()

    tmp = path.with_suffix(".jsonl.pruning")
    try:
        tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        log.info(
            "checkpoints: pruned %s from %d rows to %d", path.name, len(rows), len(kept)
        )
    except OSError:
        log.exception("checkpoints: could not prune %s; leaving it", path)
        tmp.unlink(missing_ok=True)


async def step(
    *,
    session_id: str = "",
    chat_id: str = "",
    run_id: str = "",
    boundary: str,
    tool: str = "",
    call_id: str = "",
    recovery: str = "",
    receipt: dict[str, str] | None = None,
) -> str | None:
    """Record where a turn is, without the turn ever noticing.

    The five boundaries that are not a consolidation, written from the hot
    path, so this owes two things at once: it must be DURABLE BEFORE the call
    it precedes, and it must not stall the loop that health, heartbeats and
    every other conversation share. So the append runs on a thread and the
    caller awaits it: ordering is the point of the row, and a fire-and-forget
    task could land after the effect it was written to precede.

    Never raises. A boundary the disk would not take is a row lost, and this
    is the one place in the runtime where turning that into a failed turn
    would mean the recovery mechanism breaking the work it exists to save.

    **It returns the id, or `None` when the row did not land**, and the caller
    is expected to look. Swallowing the failure AND saying nothing is what
    made a disk that would not take a note invisible: a call whose
    `before_tool` row never persisted can never be reported open, so a crash
    right after its effect leaves nothing for recovery to find and nobody is
    asked. The write still does not fail the turn, which is the rule this
    store keeps; what changes is that the caller can say so.
    """
    try:
        checkpoint = build(
            session_id=session_id,
            chat_id=chat_id,
            run_id=run_id,
            trigger="step",
            outcome="",
            state=None,
            boundary=boundary,
            tool=tool,
            call_id=call_id,
            recovery=recovery,
            receipt=receipt,
        )
        return await asyncio.to_thread(write, checkpoint)
    except Exception:  # noqa: BLE001
        log.warning("checkpoints: the %s boundary was not written", boundary, exc_info=True)
        return None
