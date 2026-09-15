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
import os
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from tesseract.mirror.server import chat_record
from tesseract.mirror.server.chat_record import (
    ChatRecord,
    chats_dir,
    default_chat_title,
    iter_history_files,
)
from tesseract.paths import home_dir

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

#: An index connection held open across a burst of writes — see ``index_batch``.
_batch = threading.local()

#: Sentinel returned by ``_with_index`` calls in ``headers()`` to tell
#: "index unreachable" apart from "index reachable and genuinely handed back
#: an empty result" — the two used to collapse onto the same ``None`` a
#: caller could not tell apart, which was defect 1: an empty index parsed
#: every file twice on every single listing.
_UNREACHABLE = object()

#: Paths `headers()` has confirmed are excluded (a channel record), keyed by
#: the record's file path as a string, value is the file's stamp (see
#: `_file_stamp`) at the moment of confirmation. A channel stem is never
#: indexed, so it would otherwise be re-parsed forever — the exact cost the
#: repair loop otherwise pays once per unreadable file. Process-local, so a
#: restart re-confirms once; guarded by a lock since `headers()` can run on
#: more than one worker thread. Invariants this cache must hold at once: (1)
#: a channel record never reaches any listing/index row through this path,
#: (2) files stay canonical, this only remembers a verdict already read from
#: one, (3) unreadable-file settling and ghost pruning are untouched by it,
#: (4) it cannot grow past the paths presently on disk, so a deleted
#: principal's entry is dropped rather than retained forever, (5) it changes
#: no I/O shape, so it adds no blocking work to the event loop, (6) it is
#: keyed by the full file path rather than the bare stem, so a chat id reused
#: under a different ``TESSERACT_HOME`` (two installs, or two homes in one
#: test process) never inherits a verdict that was read from a different
#: file.
_excluded_stems: dict[str, str] = {}
_excluded_lock = threading.Lock()


def _confirmed_excluded(path: Path, stamp: str) -> bool:
    with _excluded_lock:
        return _excluded_stems.get(str(path)) == stamp


def _mark_excluded(path: Path, stamp: str) -> None:
    with _excluded_lock:
        _excluded_stems[str(path)] = stamp


def _clear_excluded(path: Path) -> None:
    with _excluded_lock:
        _excluded_stems.pop(str(path), None)


def _prune_excluded(on_disk_paths: set[str]) -> None:
    """Drop cache entries for paths no longer on disk.

    Called with the same directory listing `headers()` already took, so this
    costs no extra walk. Keeps the cache bounded by what exists rather than
    by how many principals ever have.
    """
    with _excluded_lock:
        stale = [key for key in _excluded_stems if key not in on_disk_paths]
        for key in stale:
            del _excluded_stems[key]


def _file_stamp(stat_result: os.stat_result) -> str:
    """One freshness marker from one ``stat()``: mtime_ns, size and ctime_ns.

    mtime_ns alone is too weak — a timestamp-preserving restore
    (``shutil.copy2``, a backup tool) keeps the exact mtime of a file whose
    content changed underneath it. Size and ctime are the two other facts the
    same ``stat()`` call already carries, so folding them in costs nothing
    extra: on POSIX ctime moves on any metadata write including a bare
    ``utime``; on Windows ``st_ctime`` is creation time, which a replace or a
    copy changes but an in-place content edit or a restored mtime does not,
    so size is what catches that case there. Verified on this machine:
    ``os.scandir``'s ``DirEntry.stat()`` and ``Path.stat()`` return identical
    ``st_mtime_ns``/``st_size``/``st_ctime_ns`` for the same file, so either
    stat call may feed this and the write-through path and a scan agree.

    Accepted limit: on Windows, an edit made outside the app that keeps the
    file's exact size and then restores its mtime leaves this stamp unchanged,
    and the stale row stays until the record's next real write. Every runtime
    writer goes through ``atomic_write_text``, which always moves the mtime,
    and closing the gap would mean reading every file on every listing, which
    is the cost this index exists to avoid.
    """
    return f"{stat_result.st_mtime_ns}:{stat_result.st_size}:{stat_result.st_ctime_ns}"


def _scan_chat_files() -> dict[str, tuple[Path, str]]:
    """Every chat record stem on disk, each with its file's stamp, in one walk.

    ``os.scandir`` rather than a glob plus a separate ``stat`` per file: on
    Windows the directory enumeration already carries the file metadata, so
    ``DirEntry.stat()`` costs no extra syscall. That is what makes checking
    every file's stamp on every listing (steady state aside, where none of
    this triggers a parse) affordable — the same tree `iter_history_files()`
    walks, but with the one extra fact `headers()` needs from it.

    Admits only an entry whose stem passes ``chat_record.is_valid_chat_id`` —
    the same predicate `iter_history_files()` filters by, so the two walkers
    of this directory agree. ``atomic_write_text`` drops its temp file
    (``<random>.json``) in this same directory during every write; without
    the filter a listing concurrent with a save could pick it up as a stem
    with no record behind it.
    """
    found: dict[str, tuple[Path, str]] = {}
    try:
        scanner = os.scandir(chats_dir())
    except OSError:
        return found
    with scanner:
        for entry in scanner:
            if not entry.name.endswith(".json"):
                continue
            stem = entry.name[:-5]
            if not chat_record.is_valid_chat_id(stem):
                continue
            try:
                if not entry.is_file():
                    continue
                stamp = _file_stamp(entry.stat())
            except OSError:
                continue
            found[stem] = (Path(entry.path), stamp)
    return found


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


def _meta_row(record: ChatRecord, path: Path, stamp: str) -> Any:
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

    ``stamp`` is the caller's, never re-stat here: whoever is writing this
    row already knows which stamp it is current as of (a fresh write, or a
    directory scan `headers()` just took), and a second stat could read a
    file that moved on in between.
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
        file_stamp=stamp,
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


def upsert(record: ChatRecord, path: Path, *, stamp: str | None = None) -> None:
    """Write one record's header through to the index. Best-effort.

    Refuses a record outside the library even if a future caller forgets the
    check ``save_chat`` already makes before calling this — one enforcement
    point rather than one per caller.

    ``stamp`` is optional so the ordinary write-through path (``save_chat``,
    right after ``write_record``) need not know about reconciliation at all:
    left unset, this stats the file itself. A caller that already knows the
    stamp — ``headers()``'s repair loop, mid directory-scan — passes it, so
    the file is not stat'd twice.
    """
    if not _in_the_library(record):
        return
    if stamp is None:
        try:
            stamp = _file_stamp(path.stat())
        except OSError:
            stamp = ""
    _with_index(lambda index: index.upsert(_meta_row(record, path, stamp)), None)


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
        try:
            stamp = _file_stamp(path.stat())
        except OSError:
            stamp = ""
        rows.append(_meta_row(record, path, stamp))
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
    """One row per chat from the index, reconciled against the files, or
    ``None`` when the index cannot be reached at all.

    Invariants this function holds AT ONCE — a change here is checked
    against every one of them, not just the finding that prompted it:

    1. Files are canonical. A row only ever mirrors what a record's stamp
       (mtime_ns, size, ctime_ns — see ``_file_stamp``) says about it right
       now; the index is never the thing trusted over the file it was built
       from. mtime_ns alone is too weak a freshness marker — a
       timestamp-preserving restore keeps a stale row's exact mtime — so the
       stamp folds in size and ctime, taken from the same ``stat()`` call the
       scan already pays for.
    2. A channel record never becomes a row, whichever path found it — a
       write, a repair, or this reconciliation.
    3. In steady state — every file on disk indexed at the stamp it
       currently has — a call costs one query and zero transcript parses.
    4. A stem that is new, whose file's stamp has moved since its row was
       written, or that has no row at all is read and reconciled. A file
       that still will not parse settles at one failed parse per call,
       never a re-read of the whole corpus.
    5. The exclusion cache remembers a verdict per resolved file path, never
       per bare chat id, so it cannot leak a verdict across a
       ``TESSERACT_HOME`` change that happens to reuse an id.
    6. "The index cannot be reached" (no usable connection, this call or the
       last) degrades to ``None`` so the caller parses the records itself.
       "The index opened fine and has nothing in it yet" — a fresh install, a
       library that is nothing but channel records — is a DIFFERENT state: it
       reconciles from the files on disk and returns real rows, possibly
       ``[]``, and is never confused with the first for that reason: an empty
       result used to fall back exactly like an unreachable one, and a
       channel-only library parsed its files twice on every listing to
       relearn nothing.
    7. Archived filtering and the row's 11 keys are unchanged by any of this.
    8. The exclusion cache is bounded by what is presently on disk.
    9. Only a stem that passes ``chat_record.is_valid_chat_id`` is ever
       admitted by the directory scan — the same predicate
       ``iter_history_files()`` filters by — so a temp file
       ``atomic_write_text`` drops mid-save, or any other non-chat-id
       ``*.json`` in the directory, is never read and never logged as
       unreadable.
    """
    def _read(index: Any) -> tuple[dict[str, str], list[dict[str, Any]]]:
        return index.chat_stamps(), index.list_headers(
            include_archived=include_archived, archived_only=archived_only
        )

    def _headers(index: Any) -> list[dict[str, Any]]:
        return index.list_headers(
            include_archived=include_archived, archived_only=archived_only
        )

    outcome = _with_index(_read, _UNREACHABLE)
    if outcome is _UNREACHABLE:
        return None
    existing, rows = outcome

    on_disk = _scan_chat_files()
    _prune_excluded({str(path) for path, _stamp in on_disk.values()})

    ghosts = existing.keys() - on_disk.keys()

    # Only a stem that is new, or whose file has moved on since its row was
    # written, needs a read — the whole point of the index is to not parse
    # the corpus on every listing. A confirmed-excluded stem (a channel
    # record, most often) whose stamp has not moved needs neither.
    to_check: list[tuple[str, Path, str]] = []
    for stem, (path, stamp) in on_disk.items():
        if existing.get(stem) == stamp:
            continue
        if _confirmed_excluded(path, stamp):
            continue
        to_check.append((stem, path, stamp))

    if not ghosts and not to_check:
        return rows

    # Bulk reconciliation shares one connection and one transaction — the
    # cost of opening one is the WAL pragma and the schema check on every
    # single row otherwise, exactly the shape `index_batch` exists to avoid.
    unreadable = 0
    with index_batch():
        for chat_id in ghosts:
            forget(chat_id)
        for stem, path, stamp in to_check:
            record = chat_record.read_record(stem)
            if record is None:
                # Not a record anybody could have written a row for. It
                # stays absent and this settles: the next call re-attempts
                # one failed parse, never the whole corpus.
                unreadable += 1
                continue
            if not _in_the_library(record):
                _mark_excluded(path, stamp)
                continue
            _clear_excluded(path)
            upsert(record, path, stamp=stamp)
        # Read back inside the same held connection, before the batch
        # commits — sqlite sees a transaction's own uncommitted writes on
        # the connection that made them, so this needs no second open.
        rows = _with_index(_headers, rows)

    if unreadable:
        logger.warning(
            "chat_metadata: %d chat record(s) could not be read and are absent "
            "from the drawer", unreadable,
        )
    return rows
