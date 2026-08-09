"""AU-16 S1 — per-source rolling buffers of admitted leaves.

A buffer is an append-only newline-delimited list of leaf ids belonging
to one source (chat channel, agent, integration). ``AppendBufferJob``
writes; ``SealJob`` reads + clears once size or age crosses a threshold.

Source slugs are sanitised to safe filename characters so a source key
like ``channel:telegram:12345`` lands at
``<TESSERACT_HOME>/memory-store/leaves/buffers/channel-telegram-12345.txt``.

**Slug collision.** Two distinct source strings can canonicalise to the
same slug (``chat:a`` and ``chat-a`` both become ``chat-a``). The first
line of every buffer file is a ``# source: <original>`` header — when
``LeafBuffer.append`` opens an existing file whose header disagrees
with the current source, it logs a warning. The collision is not
treated as fatal because the seal's job is "summarise activity for
this slug" and slug collisions are rare in practice (operator-controlled
source namespace). A registry-backed strict mode lands in S2 if needed.

Coordination: ``AppendBufferJob`` and ``SealJob`` hold
``tesseract.memory.leaves.LEAF_PIPELINE_LOCK`` for their entire per-tick
pass (see that lock's docstring for why appending an id and clearing a
buffer must never interleave). Appends within that lock are still
plain atomic writes, not fcntl/msvcrt advisory locks — the process-wide
lock is what keeps two jobs from touching a buffer at once, not the
file mode. Recovery treats a torn final line as best-effort: skip the
malformed row, keep the rest.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from tesseract.memory.leaves import _resolve_home

log = logging.getLogger(__name__)


_SLUG_RE = re.compile(r"[^a-z0-9._-]+")
MAX_SLUG_LEN = 80


def buffers_root() -> Path:
    return _resolve_home() / "memory-store" / "leaves" / "buffers"


def source_slug(source: str) -> str:
    """Lower-case + ``_SLUG_RE`` substitute. Truncates to ``MAX_SLUG_LEN``
    so a runaway channel id can't exceed the filesystem's name limit.
    Empty input yields ``unknown`` so a buffer always has somewhere to
    land."""
    norm = source.strip().lower()
    norm = _SLUG_RE.sub("-", norm)
    norm = norm.strip("-._") or "unknown"
    return norm[:MAX_SLUG_LEN]


def buffer_path(source: str, *, root: Path | None = None) -> Path:
    base = (root or buffers_root()).resolve()
    return base / f"{source_slug(source)}.txt"


class LeafBuffer:
    """One per source. The on-disk file is a list of leaf ids, one per
    line. Atomic append; truncate-on-clear via ``os.replace`` of a
    freshly written tmp."""

    def __init__(self, source: str, *, root: Path | None = None) -> None:
        self.source = source
        self._path = buffer_path(source, root=root)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, leaf_id: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self._path.exists()
        if fresh:
            with self._path.open("w", encoding="utf-8") as fh:
                fh.write(f"# source: {self.source}\n")
        else:
            self._warn_on_collision()
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(f"{leaf_id}\n")

    def _warn_on_collision(self) -> None:
        """Emit a one-line warning when the on-disk header records a
        different source than this instance's. Cheap (single readline)."""
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                first = fh.readline().rstrip()
        except OSError:
            return
        prefix = "# source: "
        if not first.startswith(prefix):
            return
        recorded = first[len(prefix):].strip()
        if recorded and recorded != self.source:
            log.warning(
                "leaf buffer slug collision: %s and %s both map to %s; "
                "leaves will share a seal",
                recorded,
                self.source,
                self._path.name,
            )

    def read_ids(self) -> list[str]:
        if not self._path.exists():
            return []
        out: list[str] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            row = line.strip()
            if not row or row.startswith("#"):
                continue  # header / comment lines
            if row.startswith("leaf_"):
                out.append(row)
            else:
                log.warning(
                    "leaf buffer %s: skipping malformed row %r",
                    self._path.name,
                    row,
                )
        return out

    def size(self) -> int:
        return len(self.read_ids())

    def byte_size(self) -> int:
        try:
            return self._path.stat().st_size
        except FileNotFoundError:
            return 0

    def clear(self) -> None:
        """Replace the file with an empty one via tmp+rename so a reader
        racing the clear sees either the old content or the empty file,
        never a half-truncated read."""
        if not self._path.exists():
            return
        tmp = self._path.with_name(
            f"{self._path.stem}.{os.getpid()}.{secrets.token_hex(3)}.tmp"
        )
        tmp.write_text("", encoding="utf-8")
        os.replace(tmp, self._path)

    def stale(self, *, now: datetime, max_age_seconds: float) -> bool:
        """True when the file exists and its last mtime is older than
        ``max_age_seconds``. False on missing file (nothing to seal)."""
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            return False
        age = now.astimezone(timezone.utc).timestamp() - mtime
        return age >= max_age_seconds


def iter_buffers(*, root: Path | None = None) -> Iterator[LeafBuffer]:
    """Walk every buffer file under ``buffers_root``. Used by ``SealJob``
    so it doesn't have to track sources elsewhere — the filesystem is
    the source list."""
    base = (root or buffers_root())
    if not base.exists():
        return
    for path in base.glob("*.txt"):
        if not path.is_file():
            continue
        # Slug-only filename; we don't reverse it back to the original
        # source because nothing downstream needs the unsanitised string.
        # SealJob carries the slug as the buffer identity.
        yield LeafBuffer(path.stem, root=base)


__all__ = [
    "LeafBuffer",
    "MAX_SLUG_LEN",
    "buffer_path",
    "buffers_root",
    "iter_buffers",
    "source_slug",
]
