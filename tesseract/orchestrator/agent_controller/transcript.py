"""Append-only JSONL writer + reader for controller transcripts.

Storage: one file per session at
``<TESSERACT_HOME>/agent_controller/transcripts/<session_id>.jsonl``.

Concurrency model. Multiple controller sessions can write concurrently
because each session owns its own file — a session is a single-writer
stream from the controller's perspective. Within one session,
writes serialize through binary-append (`open(path, "ab")` + `os.fsync`)
so partial lines never interleave on Linux/macOS/Windows.

The reader provides:

* `read_from(offset)` — iterator of typed events from a byte offset.
* `tail(poll_interval)` — async generator that yields events as they
  are appended, with the offset returned by the previous call usable
  as the next `from_offset`.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import AsyncIterator, Iterator

from pydantic import BaseModel

from .events import BaseTranscriptEvent, TranscriptEvent, parse_event
from .paths import transcript_path, transcripts_dir


class TranscriptWriter:
    """Single-session JSONL appender.

    `append` is sync — callers in async contexts should wrap in
    `asyncio.to_thread` if they need to keep the event loop unblocked
    under high event volume (each append fsyncs).

    Path discipline (reviewer C-1, 2026-05-23): the target file path is
    re-resolved via :func:`transcript_path` on every write. Caching it
    at construction time would freeze a stale ``TESSERACT_HOME`` if the
    writer was instantiated before a ``monkeypatch.setenv`` (e.g. inside
    a ``scope="session"`` fixture) — the same trap the
    ``workspace_events_dir`` pattern was designed to avoid.
    """

    def __init__(self, session_id: str, *, fsync: bool = True) -> None:
        self._session_id = session_id
        self._fsync = fsync

    @property
    def path(self) -> Path:
        return transcript_path(self._session_id)

    @property
    def session_id(self) -> str:
        return self._session_id

    def append(self, event: BaseTranscriptEvent | dict) -> int:
        """Append one event. Returns the post-write byte offset."""
        if isinstance(event, BaseModel):
            payload = event.model_dump(mode="json", exclude_none=False)
        else:
            payload = dict(event)
        line = (
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode("utf-8")
        target = transcript_path(self._session_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("ab") as fh:
            fh.write(line)
            if self._fsync:
                fh.flush()
                os.fsync(fh.fileno())
        return target.stat().st_size


class TranscriptReader:
    """Replay + live-follow over a session's transcript.

    Construction does not open the file — the underlying file may not
    exist yet (a session that was created but has no events). Both
    `read_from` and `tail` handle the missing-file case as "no events yet".

    Path discipline mirrors :class:`TranscriptWriter`: the target file
    path is re-resolved via :func:`transcript_path` on every call so a
    later ``TESSERACT_HOME`` override is honored.
    """

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id

    @property
    def path(self) -> Path:
        return transcript_path(self._session_id)

    @property
    def session_id(self) -> str:
        return self._session_id

    def read_from(self, offset: int = 0) -> Iterator[tuple[TranscriptEvent, int]]:
        """Yield `(event, end_offset)` pairs starting at `offset`.

        Each `end_offset` is the byte position AFTER the yielded row —
        feed it back as `offset` to resume reading without re-replaying.
        Corrupt lines are silently skipped (matches ledger.py behavior).
        """
        target = transcript_path(self._session_id)
        if not target.exists():
            return
        with target.open("rb") as fh:
            fh.seek(offset)
            while True:
                raw = fh.readline()
                if not raw:
                    return
                end_offset = fh.tell()
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                try:
                    event = parse_event(payload)
                except Exception:
                    continue
                yield event, end_offset

    async def tail(
        self,
        *,
        from_offset: int = 0,
        poll_interval: float = 0.05,
        stop_event: asyncio.Event | None = None,
    ) -> AsyncIterator[tuple[TranscriptEvent, int]]:
        """Async generator: replay from `from_offset` then live-follow.

        Polling-based because Windows lacks reliable cross-process file
        watch primitives in the stdlib. `poll_interval` of 50ms matches
        the PTY collector cadence used elsewhere in the codebase.
        Caller can pass a `stop_event` to break out cleanly.
        """
        offset = from_offset
        while True:
            for event, end_offset in self.read_from(offset):
                yield event, end_offset
                offset = end_offset
            if stop_event is not None and stop_event.is_set():
                return
            await asyncio.sleep(poll_interval)


def transcript_exists(session_id: str) -> bool:
    return transcript_path(session_id).exists()


def list_transcripts() -> list[str]:
    """Return session ids that have at least one transcript file on disk."""
    root = transcripts_dir()
    if not root.exists():
        return []
    return sorted(p.stem for p in root.glob("*.jsonl"))


__all__ = [
    "TranscriptReader",
    "TranscriptWriter",
    "list_transcripts",
    "transcript_exists",
]
