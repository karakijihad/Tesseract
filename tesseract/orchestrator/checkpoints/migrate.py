"""The one-time fold of the per-day files into per-chat and per-run ones.

Changing a layout is a data migration whether or not anyone calls it one, and
this is that migration. It is finished work kept in its own file: it runs once
per home, it will never grow, and nobody maintaining the store's live paths has
any reason to read past it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from tesseract.orchestrator.checkpoints.layout import (
    DAILY_RE,
    MIGRATED_MARK,
    checkpoint_dir,
    checkpoint_path,
)

log = logging.getLogger(__name__)


def fold_daily_files() -> None:
    """Fold `YYYY-MM-DD.jsonl` into per-chat files, once.

    Without this the layout change is a data loss with no error in it. Every
    reader is keyed by chat, so on the day the store moved, every boundary a
    conversation had already written became unfindable: `latest_for_chat`
    returns nothing, the continuity package is rebuilt from nothing, and
    `why_not_continue` goes blind on exactly the long-running work the record
    exists for. All three fail OPEN and in silence, which is the failure this
    whole change was made to close.

    **The target is rewritten in timestamp order, not appended to.** Appending
    is what a fold that runs beside ordinary writes cannot do: an OSError
    part-way through leaves the marker unwritten and some days unfolded, a
    turn writes its own boundary into the per-chat file meanwhile, and the
    retry then puts the old rows AFTER it. `latest_for_chat` reads the
    physical tail, so the newest boundary would be one from before the layout
    changed, and tail order is the whole thing this layout rests on.

    Called before every write as well as before every read, for the same
    reason: a new boundary written into a per-chat file the fold has not
    reached yet has to be sorted in rather than sat on top of.

    Rows are deduplicated by checkpoint id, so re-entering after a crash or a
    second process cannot write a boundary twice, and a repeated boundary is
    exactly what `why_not_continue` reads as work going round.

    A row that will not parse is carried across and sorted FIRST: this is a
    move, and a move that quietly discards what it cannot read is worse than
    the truncated line it was being helpful about, but a row nothing can date
    must never end up as the tail `latest_for_chat` answers with. The daily
    file is left where it is, because deleting the only other copy of a record
    to tidy up is the wrong way round, and the retention sweep ages it on its
    newest row like everything else here.
    """
    root = checkpoint_dir()
    mark = root / MIGRATED_MARK
    if mark.exists() or not root.is_dir():
        return
    try:
        daily = sorted(p for p in root.glob("*.jsonl") if DAILY_RE.match(p.stem))
        if not daily:
            mark.write_text(_MARK_BODY, encoding="utf-8")
            return
        # Keyed by chat when there is one and by RUN when there is not, which
        # is the same rule `checkpoint_path` applies to a live write. Keying on
        # the chat alone pooled every row with no conversation behind it into
        # `_unkeyed.jsonl`, where a run-scoped read can never find it: those
        # reads open `run-<id>.jsonl` and nothing else. A task parked on a
        # scheduled run would resolve its anchor through `find`, which walks
        # every file, and then get an empty brief from the run read beside it.
        # Grouped by the TARGET PATH rather than by the pair that resolves to
        # it. A chat with fifty runs behind it resolves to one file, and
        # grouping on the pair would call `_merge_into` fifty times on that
        # file: correct, because the merge dedups and sorts, and fifty
        # read-rewrite cycles of the same file to do it.
        by_path: dict[Path, list[str]] = {}
        for path in daily:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                chat_id, run_id = _key_of(line)
                target = checkpoint_path(chat_id, run_id=run_id)
                by_path.setdefault(target, []).append(line)
        for target, lines in by_path.items():
            _merge_into(target, lines)
        mark.write_text(_MARK_BODY, encoding="utf-8")
        log.info("checkpoints: folded %d per-day files into per-chat ones", len(daily))
    except OSError:
        log.exception("checkpoints: could not fold the per-day files; leaving them")


_MARK_BODY = (
    "The per-day checkpoint files in this directory were folded into one file "
    "per conversation. They are kept, and the fold is not run again.\n"
)


def _key_of(line: str) -> tuple[str, str]:
    """The file this row belongs in, as `checkpoint_path` would decide it."""
    try:
        row = json.loads(line)
        return str(row.get("chat_id") or ""), str(row.get("run_id") or "")
    except (ValueError, AttributeError):
        return "", ""


def _id_of(line: str) -> str:
    try:
        return str(json.loads(line).get("checkpoint_id") or "")
    except (ValueError, AttributeError):
        return ""


#: What a row with no readable timestamp sorts as. Before every real one, so
#: an unreadable row is carried across and can never become the tail that
#: `latest_for_chat` answers with.
_BEFORE_EVERYTHING = datetime.min.replace(tzinfo=timezone.utc)


def _ts_of(line: str) -> datetime:
    """The row's timestamp as an INSTANT, not as the string it was written in.

    Sorting the raw string is right only while every row spells its offset the
    same way, which is true of everything this runtime writes and is not a
    property of the format: `...Z` and `...+02:00` both sort into the wrong
    place beside `...+00:00`, and the wrong place here is potentially the tail.
    """
    try:
        stamp = json.loads(line).get("ts")
    except (ValueError, AttributeError):
        return _BEFORE_EVERYTHING
    if not isinstance(stamp, str):
        return _BEFORE_EVERYTHING
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return _BEFORE_EVERYTHING
    # A row written without an offset is read as UTC, which is what this
    # runtime means by a bare timestamp everywhere else.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _merge_into(target: Path, incoming: list[str]) -> None:
    """Rewrite `target` holding its own rows and `incoming`, in time order.

    Deduplicated by checkpoint id, keeping the copy already in the target: a
    row that reached the per-chat file has been through `write` and is the one
    a reader has already seen.
    """
    existing = (
        target.read_text(encoding="utf-8").splitlines() if target.is_file() else []
    )
    rows: list[str] = []
    seen: set[str] = set()
    for line in [*existing, *incoming]:
        if not line.strip():
            continue
        row_id = _id_of(line)
        if row_id and row_id in seen:
            continue
        if row_id:
            seen.add(row_id)
        rows.append(line)
    rows.sort(key=_ts_of)
    # Written beside the target and renamed over it, so no reader ever sees a
    # half-written file. A process that dies in between leaves this behind and
    # the next fold overwrites it, which is why it is a fixed name per chat
    # rather than a unique one nothing would ever clean up.
    tmp = target.with_suffix(".jsonl.folding")
    tmp.write_text("\n".join(rows) + "\n", encoding="utf-8")
    tmp.replace(target)
