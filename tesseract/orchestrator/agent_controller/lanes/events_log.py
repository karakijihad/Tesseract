"""Append-only `events.jsonl` writer + cursor-based reader.

Cursor is the byte offset of the next unread line. The single
`LaneManager` instance per controller daemon owns the writer (one
writer per file); readers are concurrent + idempotent.

Wire format: one JSON object per line, UTF-8 encoded, LF-terminated.
The writer pre-stamps `cursor=""` to keep line bytes stable; readers
fill in the cursor from the actual byte offset observed at read time.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .models import LaneEvent


@dataclass(frozen=True)
class LaneEventsCursor:
    """Opaque cursor returned by reads. Wire form is the decimal string of
    `byte_offset`; we wrap it in a dataclass for type-safety inside the
    manager and serialize to/from the string at the IPC boundary."""

    byte_offset: int

    @classmethod
    def parse(cls, raw: str | None) -> "LaneEventsCursor":
        if raw is None or raw == "":
            return cls(0)
        try:
            offset = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid lane cursor {raw!r}") from exc
        if offset < 0:
            raise ValueError(f"lane cursor must be non-negative, got {offset}")
        return cls(offset)

    @property
    def wire(self) -> str:
        return str(self.byte_offset)


def append_event(events_path: Path, event: LaneEvent) -> int:
    """Append one event to `events.jsonl`; return the post-write file
    size (which is the next reader's cursor lower bound).

    The cursor field on the input event is ignored on write — readers
    stamp it from observed byte offsets. ``model_dump`` with
    ``mode="json"`` ensures any nested datetime/enum are wire-safe.
    """
    payload = event.model_dump(mode="json")
    payload["cursor"] = ""
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoded = (line + "\n").encode("utf-8")
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("ab") as fh:
        fh.write(encoded)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            # Best-effort durability; ignore on filesystems that don't
            # support fsync (e.g. some Windows network shares).
            pass
        return fh.tell()


def read_events_since(
    events_path: Path, cursor: LaneEventsCursor
) -> tuple[list[LaneEvent], LaneEventsCursor]:
    """Read every full line from `cursor.byte_offset` to EOF and parse
    each into a `LaneEvent`. Stamps each event's `cursor` with the byte
    offset of THAT line's start (the value a future reader would pass
    to skip past it).

    Returns `(events, next_cursor)`. `next_cursor.byte_offset` is the
    file size at read time — passing it back yields zero events until a
    new write lands.

    Idempotent on `cursor`: calling twice with the same cursor returns
    the same events (modulo any new appends between calls).

    Lines that fail JSON parse or Pydantic validation are skipped with
    no exception raised — the cursor still advances past the bad line
    so a transient malformed write doesn't wedge the reader forever.
    """
    if not events_path.exists():
        return [], cursor
    out: list[LaneEvent] = []
    with events_path.open("rb") as fh:
        fh.seek(cursor.byte_offset)
        offset = cursor.byte_offset
        while True:
            line = fh.readline()
            if not line:
                break
            line_start = offset
            offset += len(line)
            stripped = line.rstrip(b"\n").rstrip(b"\r")
            if not stripped:
                continue
            try:
                raw = json.loads(stripped.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            raw["cursor"] = str(line_start)
            try:
                event = LaneEvent.model_validate(raw)
            except Exception:  # noqa: BLE001 — bad event must not wedge reader
                continue
            out.append(event)
        # offset is now EOF as observed during the read.
    return out, LaneEventsCursor(offset)


__all__ = [
    "LaneEventsCursor",
    "append_event",
    "read_events_since",
]
