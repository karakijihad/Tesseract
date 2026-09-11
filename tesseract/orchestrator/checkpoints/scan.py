"""The questions a caller cannot name a file for.

A process that has just started knows no conversation names, and a turn record
carries a run id and no chat id. Both have to walk, and both are bounded by the
prune ceiling rather than by a window, because what they are looking for is the
old row nothing ever closed.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Iterator

from tesseract.orchestrator.checkpoints.layout import (
    DAILY_RE,
    WHOLE_FILE,
    WRITE_LOCK,
    checkpoint_dir,
)
from tesseract.orchestrator.checkpoints.migrate import fold_daily_files
from tesseract.orchestrator.checkpoints.read import row_ts, tail, unclosed_in
from tesseract.orchestrator.checkpoints.record import Checkpoint

log = logging.getLogger(__name__)


def unresolved(*, before: datetime | None = None, per_key: int = WHOLE_FILE) -> list[Checkpoint]:
    """Every call this machine opened and never closed, across every file.

    What a boot-time pass reads, and the reason it cannot use `open_calls`:
    that one answers about a conversation you already know the name of, and a
    process that has just started knows none of them. The dead run's own file
    is the only place its name survives.

    `before` is the liveness line, and it has to be given rather than assumed.
    A recovery pass hands it this boot's start, so a call THIS process is
    making right now is not reported as the wreckage of the last one. It is
    the same rule `recovery/manager.py::_scan_turns` applies to an open turn
    manifest, said in timestamps because a checkpoint carries no boot id.

    Newest first. `per_key` is the whole of a file rather than a window: the
    prune keeps every file under `MAX_BYTES` and never drops an unclosed
    row, so reading it all is bounded by construction, and a window would
    have hidden exactly the old open call this is looking for behind newer
    traffic in the same conversation.

    **The fold runs first and the per-day files are skipped.** They are left
    on disk after the layout change and aged by retention, and `_merge_into`
    copies their rows verbatim into the per-chat file, so a walk that took
    both would count every open call from before the change twice and put it
    in front of the operator twice.
    """
    out: list[Checkpoint] = []
    for path in files():
        for row in unclosed_in(tail(path, want=per_key)):
            if before is not None and row_ts(row) >= before:
                continue
            out.append(row)
    out.sort(key=row_ts, reverse=True)
    return out


def files() -> "Iterator[Path]":
    """Every per-chat and per-run file, folded first, in a stable order.

    The walk `unresolved` does, lifted out because two more questions need it
    and each needs it for the same reason: **a caller holding a turn record or
    a checkpoint id cannot name the file.** A turn's rows land in its
    CONVERSATION's file when it had one, and `RunManifest` carries `run_id` and
    `entry` and no chat id, by design, because a manifest is a record of a run
    and not of a conversation. The only place a dead run's name survives is the
    rows it wrote.

    Bounded by construction: the prune keeps every file under `MAX_BYTES`.

    The per-day files are skipped for the reason `unresolved` skips them: the
    fold copies their rows verbatim into the per-chat files, so walking both
    would read every pre-migration row twice.
    """
    with WRITE_LOCK:
        fold_daily_files()
    root = checkpoint_dir()
    if not root.is_dir():
        return
    for path in sorted(root.glob("*.jsonl")):
        if not DAILY_RE.match(path.stem):
            yield path


def walk(per_key: int = WHOLE_FILE) -> "Iterator[Checkpoint]":
    """Every row this machine kept, newest first within each file."""
    for path in files():
        yield from tail(path, want=per_key)


def find(checkpoint_id: str) -> Checkpoint | None:
    """One row by its own id, wherever it landed.

    What `AgendaItem.current_checkpoint` resolves to. The field is a reference
    and this is the only thing that makes it one: without a reader the id would
    be a value written and never looked at.
    """
    if not checkpoint_id:
        return None
    for row in walk():
        if row.checkpoint_id == checkpoint_id:
            return row
    return None


def latest_for_run(run_id: str) -> Checkpoint | None:
    """The newest row this RUN wrote, wherever it landed.

    `latest_step` answers the same question and needs to be told which file to
    open. This one is for the caller that has only a turn record.
    """
    if not run_id:
        return None
    newest: Checkpoint | None = None
    for row in walk():
        if row.run_id != run_id:
            continue
        if newest is None or row_ts(row) > row_ts(newest):
            newest = row
    return newest
