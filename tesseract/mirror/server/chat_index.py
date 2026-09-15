"""The derived index over the chat records — a cache of their headers.

`memory/chat_metadata.py` holds one row per record so a listing does not parse
every transcript on every open. The records are canonical; this
is rebuildable from them at any time, which is why every path here degrades to
``None`` or a default rather than failing a write.

This module owns the connection, the batching, the row shape and the
completeness check. It never decides what a record CONTAINS — title,
snippet, message count are always ``chat_store``'s own computation, reused
here rather than copied. It does enforce the one boundary condition
``chat_store.save_chat`` already draws before it ever calls ``upsert``: a
channel record is never a row here, whichever path found it — a write, a
repair, or a rebuild.

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
from tesseract.mirror.server.chat_record import (
    ChatRecord,
    chat_path,
    default_chat_title,
    iter_history_files,
)
from tesseract.paths import home_dir

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

#: An index connection held open across a burst of writes — see ``index_batch``.
_batch = threading.local()

#: Stems `headers()` has confirmed are excluded (a channel record), keyed by
#: stem, value is the file's mtime at the moment of confirmation. A channel
#: stem is never indexed, so it is in `missing` on every single call, and
#: without this it would be re-parsed forever — the exact cost the repair
#: loop otherwise pays once per unreadable file. Process-local, so a restart
#: re-confirms once; guarded by a lock since `headers()` can run on more than
#: one worker thread. Invariants this cache must hold at once: (1) a channel
#: record never reaches any listing/index row through this path, (2) files
#: stay canonical, this only remembers a verdict already read from one, (3)
#: unreadable-file settling and ghost pruning are untouched by it, (4) it
#: cannot grow past the stems presently on disk, so a deleted principal's
#: entry is dropped rather than retained forever, (5) it changes no I/O
#: shape, so it adds no blocking work to the event loop.
_excluded_stems: dict[str, int] = {}
_excluded_lock = threading.Lock()


def _confirmed_excluded(stem: str, mtime_ns: int) -> bool:
    with _excluded_lock:
        return _excluded_stems.get(stem) == mtime_ns


def _mark_excluded(stem: str, mtime_ns: int) -> None:
    with _excluded_lock:
        _excluded_stems[stem] = mtime_ns


def _clear_excluded(stem: str) -> None:
    with _excluded_lock:
        _excluded_stems.pop(stem, None)


def _prune_excluded(on_disk: set[str]) -> None:
    """Drop cache entries for stems no longer on disk.

    Called with the same directory listing `headers()` already took, so this
    costs no extra walk. Keeps the cache bounded by what exists rather than
    by how many principals ever have.
    """
    with _excluded_lock:
        stale = [stem for stem in _excluded_stems if stem not in on_disk]
        for stem in stale:
            del _excluded_stems[stem]


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
    """The row one record writes through as. The only place this is built,
    so the write-through path (``upsert``) and the rebuild path
    (``_rows_from_disk``) can never compute ``message_count``/``snippet``/
    ``last_active_at`` two different ways.

    ``first_operator_text`` and ``_last_active_stamp`` are imported from
    ``chat_store`` rather than reimplemented here: they are the SAME
    computation ``list_chats``' parse fallback uses to build a row, and a
    second copy is exactly how the retiring ``header_from_record`` drifted
    from the row it was meant to mirror. Deferred import: ``chat_store``
    imports this module at load time, so importing it back at module scope
    here would be circular; by the time anything calls this function
    ``chat_store`` is already fully loaded.
    """
    from tesseract.memory.chat_metadata import ChatMetaRow
    from tesseract.mirror.server.chat_store import first_operator_text, _last_active_stamp

    born = default_chat_title(record.created_at or "")
    return ChatMetaRow(
        chat_id=record.chat_id,
        title=record.title,
        created_at=record.created_at,
        started_at=record.started_at,
        ended_at=record.ended_at,
        turn_count=record.turn_count,
        model=record.model,
        archived=record.archived,
        message_count=len(record.history),
        snippet=first_operator_text(record) if record.title == born else "",
        last_active_at=_last_active_stamp(record),
        file_path=str(path),
    )


def _in_the_library(record: ChatRecord) -> bool:
    """Whether this record belongs in the index at all.

    Mirrors ``chat_store.save_chat``'s own write-through gate exactly
    (``NOT_IN_THE_LIBRARY``) rather than a second definition of "the library"
    that could drift from it. A channel record IS on disk and its stem IS
    found by every directory walk here, but it is the bridge's own restore
    state, not a conversation the cockpit's drawer, the recall index, or a
    listing may ever surface — the same reason ``chat_store._walk`` excludes
    it by default.
    """
    from tesseract.mirror.server.chat_store import NOT_IN_THE_LIBRARY

    return record.surface not in NOT_IN_THE_LIBRARY


def upsert(record: ChatRecord, path: Path) -> None:
    """Write one record's header through to the index. Best-effort.

    Refuses a record outside the library even if a future caller forgets the
    check ``save_chat`` already makes before calling this — one enforcement
    point rather than one per caller.
    """
    if not _in_the_library(record):
        return
    _with_index(lambda index: index.upsert(_meta_row(record, path)), None)


def forget(chat_id: str) -> None:
    """Drop one record's row. Best-effort."""
    _with_index(lambda index: index.delete(chat_id), None)


def _rows_from_disk() -> tuple[list[Any], int]:
    """Every record the walker can parse, and how many files it could not.

    The second number is what keeps ``headers``' completeness check honest.
    A file that will not parse is not a row anybody could have written, so
    counting it as a missing row would condemn the index for a record that
    does not exist. A channel record parses fine and is skipped for a
    different reason (``_in_the_library``), so it is counted as neither.
    """
    rows: list[Any] = []
    unreadable = 0
    for path in iter_history_files():
        record = chat_record.read_record(path.stem)
        if record is None:
            unreadable += 1
            continue
        if not _in_the_library(record):
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
    _prune_excluded(on_disk)

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
    #
    # A channel record settles the OTHER way: it parses fine, every single
    # call, and is never indexed, so it stays in `missing` forever. Re-parsing
    # it on every listing would be the one-file cost `headers` already accepts
    # for a file that will not parse at all, paid instead on every call by
    # however many channel principals the bridge has. `_excluded_stems`
    # remembers the verdict once it is confirmed and skips the parse until the
    # file's mtime moves, which is the only event that can change the verdict.
    repaired = 0
    excluded = 0
    for stem in missing:
        path = chat_path(stem)
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            mtime_ns = None
        if mtime_ns is not None and _confirmed_excluded(stem, mtime_ns):
            excluded += 1
            continue
        record = chat_record.read_record(stem)
        if record is None:
            continue
        if not _in_the_library(record):
            excluded += 1
            if mtime_ns is not None:
                _mark_excluded(stem, mtime_ns)
            continue
        _clear_excluded(stem)
        upsert(record, path)
        repaired += 1
    if repaired + excluded < len(missing):
        logger.warning(
            "chat_metadata: %d chat record(s) could not be read and are absent "
            "from the drawer", len(missing) - repaired - excluded,
        )
    if not repaired and not ghosts:
        return rows
    return _with_index(_headers, None)
