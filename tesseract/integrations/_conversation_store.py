"""Per-channel conversation store — append-only JSONL of inbound/outbound messages.

Layout (MO-9-10 architecture decision):

    <TESSERACT_HOME>/logs/channels/<channel>/<chat_id>/conversations.jsonl

Rows are :class:`ChannelMessage` dicts. The store is the canonical surface
the Mirror Channels Conversations pane (MO-9-12) reads from — channels do
*not* write into ``workspace_events`` any more (the workspace tab is the
operator's personal scratchpad, not a feed into external chats).

Concurrency: each append takes a per-instance ``threading.Lock`` plus an
advisory cross-process lock on ``<chat_id>/.lock`` — same shape as
:class:`tesseract.workspace_events.events.EventStore`. The cross-process
lock matters because the Mirror, REPL, and (future) scheduler-driven
maintenance might write to the same chat file from different processes.
``TESSERACT_HOME`` is resolved at call time so test fixtures that
``monkeypatch.setenv`` after import still hit the temp dir.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import IO, Any, Iterator

from tesseract.integrations._channel_adapter import ChannelMessage
from tesseract.paths import TESSERACT_HOME, log_dir

log = logging.getLogger(__name__)


def _channels_root() -> Path:
    root = Path(os.environ.get("TESSERACT_HOME") or TESSERACT_HOME).resolve()
    out = log_dir("channels")
    out.mkdir(parents=True, exist_ok=True)
    return out


def _chat_dir(channel: str, chat_id: str) -> Path:
    safe_channel = _safe_segment(channel)
    safe_chat = _safe_segment(chat_id)
    out = _channels_root() / safe_channel / safe_chat
    out.mkdir(parents=True, exist_ok=True)
    return out


def _safe_segment(value: str) -> str:
    """Strip path separators / dotted prefixes so a malicious channel-name
    (or future chat_id ingestion) cannot escape the channels root.

    Channel names are operator-curated today (``telegram``); chat IDs are
    integers stringified. The guard is belt + braces — empty / unsafe
    inputs collapse to ``unknown`` so a row still lands and surfaces in
    logs rather than vanishing silently.
    """
    cleaned = "".join(ch for ch in str(value) if ch.isalnum() or ch in {"-", "_"})
    return cleaned or "unknown"


class ConversationStore:
    """Append-only JSONL writer keyed by ``(channel, chat_id)``.

    One instance is fine for the whole Mirror process; the cross-process
    advisory lock handles writers in sibling processes (REPL, future
    maintenance scripts). Instances hold no mutable state between calls
    apart from the in-process ``Lock``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def append(self, channel: str, chat_id: str, message: ChannelMessage) -> None:
        """Append one message row to the per-day file for ``ts``'s date.

        2026-05-17: rotated from one monolithic ``conversations.jsonl`` per
        chat to ``conversations/YYYY-MM-DD.jsonl`` so the per-chat tree
        stays bounded and the assistant can read a specific day cheaply via
        :meth:`day_rows`. The legacy file still exists for chats not yet
        migrated; :meth:`tail` and :meth:`day_rows` both fall back to it
        when no per-day files are present.

        Creates the parent directory on first call. Locking semantics
        match :class:`EventStore`: in-process lock + advisory file lock on
        ``.lock`` so concurrent writers in other processes serialise.
        """
        chat_dir = _chat_dir(channel, chat_id)
        day_dir = chat_dir / "conversations"
        day_dir.mkdir(parents=True, exist_ok=True)
        date_part = _date_from_ts(message.ts)
        path = day_dir / f"{date_part}.jsonl"
        lock_path = chat_dir / ".lock"
        row = asdict(message)
        with self._lock, _interprocess_lock(lock_path):
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")

    def tail(
        self,
        channel: str,
        chat_id: str,
        *,
        limit: int = 100,
        before_iso: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the most recent ``limit`` rows, newest-first.

        Walks per-day files (newest date first) until ``limit`` rows are
        collected. ``before_iso``: cap to rows strictly older than this
        ISO timestamp (matches the protocol's
        ``list_conversation(before_iso=...)`` cursor). Malformed rows
        are skipped with a warning rather than aborting the read so a
        single torn write does not blank the pane.

        Backward-compat: when a chat has no per-day files, reads the
        legacy ``conversations.jsonl`` directly.
        """
        if limit < 0:
            limit = 0
        if limit == 0:
            return []
        chat_dir = _chat_dir(channel, chat_id)
        day_files = _day_files(chat_dir, descending=True)
        if not day_files:
            legacy = chat_dir / "conversations.jsonl"
            if not legacy.exists():
                return []
            day_files = [legacy]

        rows: list[dict[str, Any]] = []
        for path in day_files:
            for row in _read_jsonl_rows(path):
                if before_iso is not None:
                    ts = row.get("ts")
                    if isinstance(ts, str) and ts >= before_iso:
                        continue
                rows.append(row)
            # Short-circuit counts ONLY filtered-in rows (the pre-filter
            # check above is now inside the inner loop, so ``len(rows)``
            # already excludes anything ``before_iso`` would drop).
            # Threshold = limit * 2 to absorb any reorder noise after
            # sort. Without this filter-aware counting, a chat where the
            # newest file is all >= before_iso would still bail after
            # one file even though zero rows passed (B14 fix).
            if len(rows) >= limit * 2:
                break
        rows.sort(key=lambda r: str(r.get("ts") or ""), reverse=True)
        return rows[:limit]

    def day_rows(
        self, channel: str, chat_id: str, *, date: str,
    ) -> list[dict[str, Any]]:
        """Return every row from ``<chat>/conversations/<date>.jsonl``
        oldest-first (chronological). Empty list when the day has no
        file. Convenience surface for :class:`ChannelHistoryReadTool`.
        """
        chat_dir = _chat_dir(channel, chat_id)
        path = chat_dir / "conversations" / f"{date}.jsonl"
        if not path.exists():
            return []
        return _read_jsonl_rows(path)

    def list_days(self, channel: str, chat_id: str) -> list[str]:
        """Newest-first list of ``YYYY-MM-DD`` strings with archived data."""
        chat_dir = _chat_dir(channel, chat_id)
        day_dir = chat_dir / "conversations"
        if not day_dir.exists():
            return []
        days: list[str] = []
        for path in day_dir.iterdir():
            if path.is_file() and path.suffix == ".jsonl":
                days.append(path.stem)
        days.sort(reverse=True)
        return days


def _date_from_ts(ts: str) -> str:
    """Parse the date portion of an ISO timestamp; quarantine on parse fail.

    Matches the migration-script bucket: unparseable rows route to
    ``0000-00-00.jsonl`` so the operator sees the anomaly cleanly
    instead of having it silently smear into today's file (which would
    confuse a later migration pass on a partially-rotated chat).
    """
    if isinstance(ts, str) and len(ts) >= 10:
        head = ts[:10]
        if len(head) == 10 and head[4] == "-" and head[7] == "-":
            return head
    return "0000-00-00"


def _day_files(chat_dir: Path, *, descending: bool) -> list[Path]:
    day_dir = chat_dir / "conversations"
    if not day_dir.exists():
        return []
    files = [p for p in day_dir.iterdir() if p.is_file() and p.suffix == ".jsonl"]
    files.sort(key=lambda p: p.name, reverse=descending)
    return files


def _read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("channels conversations unreadable %s: %s", path, exc)
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            log.warning("channels conversations bad row in %s: %s", path, exc)
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


@contextmanager
def _interprocess_lock(lock_path: Path) -> Iterator[None]:
    """Advisory cross-process exclusive lock on ``lock_path``.

    Falls open silently when the platform primitive is unavailable —
    better to risk an interleaved append than crash the writer (this
    matches :class:`EventStore` behaviour).
    """
    fh: IO[bytes] | None = None
    locked = False
    try:
        fh = open(lock_path, "a+b")
        if sys.platform == "win32":
            try:
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                locked = True
            except (OSError, ImportError):
                log.warning("conversation store: msvcrt lock unavailable; falling back")
        else:
            try:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                locked = True
            except (OSError, ImportError):
                log.warning("conversation store: fcntl lock unavailable; falling back")
        try:
            yield
        finally:
            if locked:
                try:
                    if sys.platform == "win32":
                        import msvcrt

                        try:
                            fh.seek(0)
                        except OSError:
                            pass
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    log.exception("conversation store: lock release failed")
    finally:
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass
