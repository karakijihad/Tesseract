"""The derived index over the chat records — a cache of their headers.

`memory/chat_metadata.py` holds one row per record so a listing does not parse
every transcript on every open. The records are canonical; this
is rebuildable from them at any time, which is why every path here degrades to
``None`` or a default rather than failing a write.

This module owns the connection, the batching, the row shape and the
completeness check. It never decides what a record contains.

Its surface is five functions — ``index_batch``, ``upsert``, ``forget``,
``headers``, ``rebuild_metadata_index``. Everything else is private,
including the
connection helper: a caller reaching past ``upsert``/``forget`` to run its own
action against the index is the second owner this module exists to prevent.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from tesseract.mirror.server import chat_record
from tesseract.mirror.server.chat_record import ChatRecord, chat_path, iter_history_files
from tesseract.paths import home_dir

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

#: An index connection held open across a burst of writes — see ``index_batch``.
_batch = threading.local()


def metadata_index_path() -> Path:
    """``<TESSERACT_HOME>/chat_metadata.sqlite``, resolved at call time.

    Resolved at call time like ``chats_dir``, so a test fixture setting
    ``TESSERACT_HOME`` gets an isolated index rather than the operator's.
    """
    return home_dir() / "chat_metadata.sqlite"


def _with_index(action: Callable[[Any], _T], default: _T) -> _T:
    """Run one action against the derived index. Best-effort, always closed.

    The index is derived and rebuildable, so a failure here must never cost a
    write to the canonical record — every caller passes what it wants back
    when the index is unreachable.

    Inside an ``index_batch`` the held connection is reused instead of opened.
    """
    held = getattr(_batch, "index", None)
    if held is not None:
        try:
            return action(held)
        except Exception:  # noqa: BLE001
            logger.warning("chat_metadata: index action failed", exc_info=True)
            return default
    try:
        from tesseract.memory.chat_metadata import ChatMetadataIndex

        index = ChatMetadataIndex(metadata_index_path())
    except Exception:  # noqa: BLE001
        return default
    try:
        return action(index)
    except Exception:  # noqa: BLE001
        logger.warning("chat_metadata: index action failed", exc_info=True)
        return default
    finally:
        try:
            index.close()
        except Exception:  # noqa: BLE001
            pass


@contextmanager
def index_batch():
    """Hold one index connection open across a burst of writes.

    Opening one costs ~8 ms — the WAL pragma and the schema check, not the
    query — and ``persist_session_chats`` saves every chat in a session on a
    timer, ON THE EVENT LOOP. Per-chat that is 8 ms times however many
    conversations the operator has open, which crosses the 50 ms bar that
    keeps health checks and inbound turns responsive; per burst it is 8 ms
    once. Thread-local because a sqlite connection belongs to the thread that
    opened it.

    Re-entering reuses the connection already held rather than opening a second
    one — a nested batch replacing it would leak the outer connection and end
    its transaction early.
    """
    if getattr(_batch, "index", None) is not None:
        yield
        return
    try:
        from tesseract.memory.chat_metadata import ChatMetadataIndex

        _batch.index = ChatMetadataIndex(metadata_index_path())
    except Exception:  # noqa: BLE001
        _batch.index = None
    index = getattr(_batch, "index", None)
    if index is None:
        try:
            yield
        finally:
            _batch.index = None
        return
    try:
        with index.deferred():
            yield
    finally:
        _batch.index = None
        try:
            index.close()
        except Exception:  # noqa: BLE001
            pass


def _meta_row(record: ChatRecord, path: Path) -> Any:
    from tesseract.memory.chat_metadata import ChatMetaRow

    return ChatMetaRow(
        chat_id=record.chat_id,
        title=record.title,
        created_at=record.created_at,
        started_at=record.started_at,
        ended_at=record.ended_at,
        turn_count=record.turn_count,
        model=record.model,
        archived=record.archived,
        file_path=str(path),
    )


def upsert(record: ChatRecord, path: Path) -> None:
    """Write one record's header through to the index. Best-effort."""
    _with_index(lambda index: index.upsert(_meta_row(record, path)), None)


def forget(chat_id: str) -> None:
    """Drop one record's row. Best-effort."""
    _with_index(lambda index: index.delete(chat_id), None)


def _rows_from_disk() -> tuple[list[Any], int]:
    """Every record the walker can parse, and how many files it could not.

    The second number is what keeps ``headers``' completeness check honest.
    A file that will not parse is not a row anybody could have written, so
    counting it as a missing row would condemn the index for a record that
    does not exist.
    """
    rows: list[Any] = []
    unreadable = 0
    for path in iter_history_files():
        record = chat_record.read_record(path.stem)
        if record is None:
            unreadable += 1
            continue
        rows.append(_meta_row(record, path))
    return rows, unreadable


def rebuild_metadata_index() -> int:
    """Rebuild the derived index from the records on disk. Returns the count.

    The walk is the record layer's, not the index's — one owner of the
    directory, rather than one reader per consumer.
    """
    rows, _ = _rows_from_disk()
    return _with_index(lambda index: index.replace_all(rows), 0)


def headers(
    *, include_archived: bool, archived_only: bool
) -> list[dict[str, Any]] | None:
    """One row per chat from the index, or ``None`` to read the records.

    ``None`` when the index is unreachable, empty, or short of the records on
    disk. That last check is what makes the fast path safe to trust: nothing
    rebuilds this index on a schedule, so a row that never arrived — a burst
    left uncommitted by a kill, a file dropped in by hand — would hide a
    conversation from the drawer indefinitely, and a fallback on an EMPTY
    result cannot see a listing that is merely short. Counting the files is a
    directory listing; the parse is what the index exists to avoid.

    A shortfall is REPAIRED rather than merely detected. The first version fell
    back forever, and a single unparseable file — which no rebuild can turn
    into a row — left the drawer parsing every transcript on every open, with
    a warning line and no way back. So a mismatch reconciles by id and asks
    what it could actually see: when the index then holds every record that
    exists, the remaining difference is unreadable files, and the index is as
    complete as anything can make it.
    """
    def _read(index: Any) -> tuple[set[str], list[dict[str, Any]]]:
        return index.chat_ids(), index.list_headers(
            include_archived=include_archived, archived_only=archived_only
        )

    def _headers(index: Any) -> list[dict[str, Any]]:
        return index.list_headers(
            include_archived=include_archived, archived_only=archived_only
        )

    indexed, rows = _with_index(_read, (set(), []))
    if not indexed:
        return None
    on_disk = {path.stem for path in iter_history_files()}

    ghosts = indexed - on_disk
    if ghosts:
        _with_index(lambda index: [index.delete(cid) for cid in ghosts], None)

    missing = on_disk - indexed
    if not ghosts and not missing:
        return rows

    # Repair only what is missing, never the whole corpus — the whole point of
    # the index is not to parse the corpus. A stem that STILL will not parse is
    # not a record anybody could have written a row for, so it stays absent and
    # this settles: the next call re-attempts one failed json parse rather than
    # re-reading every transcript.
    repaired = 0
    for stem in missing:
        record = chat_record.read_record(stem)
        if record is None:
            continue
        upsert(record, chat_path(stem))
        repaired += 1
    if repaired < len(missing):
        logger.warning(
            "chat_metadata: %d chat record(s) could not be read and are absent "
            "from the drawer", len(missing) - repaired,
        )
    if not repaired and not ghosts:
        return rows
    return _with_index(_headers, None)
