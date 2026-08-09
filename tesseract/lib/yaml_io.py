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


def load_round_trip(path: Path) -> Any:
    """Load `path` preserving comments and quote styles, without writing.

    For reading a document whose *formatting* is part of what the caller
    needs — `config_seed.migrate_config_keys` copies a subtree out of a
    shipped template and into the operator's file, and the comments
    explaining that block travel with the node.
    """
    with path.open("r", encoding="utf-8") as fh:
        return _round_trip_yaml().load(fh)
