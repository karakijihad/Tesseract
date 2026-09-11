"""Shared YAML / file-IO helpers — single source of truth for the
ruamel round-trip pattern and atomic-write boilerplate.

Phase 15X consolidates three identical copies that lived in
`mirror/server/routes/settings.py`, `scheduler/config_loader.py`, and
`scheduler/alarms.py` into one helper. The behavior is bit-identical —
tempfile in the same directory, `os.replace` for atomicity, cleanup on
error, ruamel mapping=2/sequence=4/offset=2 indent.

Two public helpers:

- `atomic_write_text(path, text)` — write text via a sibling tempfile
  + `os.replace`. The caller controls encoding/format. Used by alarm
  persistence (safe-yaml dump) and by `round_trip_yaml` below.
- `round_trip_yaml(path, mutate)` — load `path` with ruamel,
  mutate the parsed doc in place, atomically write it back. Returns
  the mutated doc. Operator comments + key order are preserved.
"""

from __future__ import annotations

import io
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from ruamel.yaml import YAML


def atomic_write_text(path: Path, text: str, *, prefix: str = "") -> None:
    """Atomically replace `path` with `text`.

    Strategy: write to a tempfile in the same directory (so `os.replace`
    is atomic on the same filesystem), then rename. On any error, the
    tempfile is unlinked best-effort so we don't leave orphans.

    `prefix` defaults to "" (matches `tempfile.mkstemp`'s default).
    Pass a hint like ".schedule-" if you want the tempfile to be
    obvious in `ls -la` during a debug session.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=prefix, suffix=path.suffix or ".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


#: One lock per file, so a read-mutate-write of a config file cannot be
#: interleaved with another of the SAME file.
#:
#: `os.replace` makes each write atomic, which was mistaken for making the
#: round trip atomic. It does not: the load, the mutation and the write are
#: three steps, and a second writer that completes inside that span has its
#: change replaced by a document built from a read taken before it. Two
#: writers of `roles.yaml` exist today and they are not even on the same
#: thread: the cockpit's cost route runs on the event loop, and a tuning
#: card's approve runs on a worker through `asyncio.to_thread`. A third,
#: `set_role_models`, is a third door to the same file.
#:
#: Per path rather than one global lock, because these files are unrelated
#: and serialising every config write behind one would make an unrelated
#: slow write everybody's problem. Keyed on the resolved path so two spellings
#: of one file cannot take two different locks.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(Path(path).resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.Lock()
    return lock


def round_trip_yaml(path: Path, mutate: Callable[[Any], None]) -> Any:
    """Load `path` with ruamel (preserving quotes + comments), apply
    `mutate(doc)` to the root document in place, then atomically write
    it back. Returns the mutated doc so callers can re-parse fields they
    just wrote.

    The ruamel indent settings (`mapping=2, sequence=4, offset=2`) are
    pinned so every consolidated call site produces identical on-disk
    output. The `path.exists()` precondition is left to the caller —
    different sites want different errors on miss (FileNotFoundError vs
    HTTP 404).

    `width` is pinned wide because ruamel's default (80) re-wraps any
    line longer than that — including flow sequences the operator wrote
    on one line, which come back split across two with a trailing space.
    A rename from the Identity tab was reflowing unrelated blocks of
    mirror.yaml that way; a write must change the key it was asked to
    change and nothing else.
    """
    ryaml = _round_trip_yaml()
    with _lock_for(path):
        with path.open("r", encoding="utf-8") as fh:
            doc = ryaml.load(fh)
        mutate(doc)
        buf = io.StringIO()
        ryaml.dump(doc, buf)
        atomic_write_text(path, buf.getvalue())
    return doc


def _round_trip_yaml() -> YAML:
    """The pinned ruamel configuration, in one place so a document loaded
    for reading and a document written back cannot disagree about quoting,
    indentation, or wrap width."""
    ryaml = YAML()
    ryaml.preserve_quotes = True
    ryaml.width = 4096
    ryaml.indent(mapping=2, sequence=4, offset=2)
    return ryaml


