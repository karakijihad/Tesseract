"""Canvas-state file I/O — shared by the surface store and the Mirror
``canvas_state`` route.

One file per view at ``<TESSERACT_HOME>/workspace/canvas-state/<view>.json``
(operator-private, gitignored). The blob is the Surface Protocol persistence
envelope: ``schema_version``, ``view``, ``saved_at_utc``, ``viewport``,
``surfaces`` (backend-owned, this layer), ``tldraw_snapshot``
(frontend-owned, opaque here).

The two owners write disjoint keys (frontend: ``tldraw_snapshot`` +
``viewport``; backend: ``surfaces``), and every writer does a read-merge
that preserves the *other* owner's key. Safety rests on two invariants:
(1) each read-modify-write below is fully synchronous — there is no
``await`` between ``read_view_blob`` and ``write_view_blob`` — so under the
single Mirror event loop one writer's RMW runs to completion before the
other's begins (no interleave); (2) the write is atomic at the FS layer
(``os.replace`` over a ``.tmp``). Because each writer preserves the other's
key, *either* completion order leaves both keys intact. This holds for the
single-process Mirror backend; a second writing process would need a file
lock. ``TESSERACT_HOME`` is resolved at call time so tests that
``monkeypatch.setenv`` retarget the writer at a tmp_path (canonical
pattern: ``kernel/workspace_changes.py::workspace_events_dir``).
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tesseract.paths import workspace_dir

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_VIEW_RE = re.compile(r"[a-zA-Z0-9_-]{1,64}")

# Source-controlled baseline layouts (Y-3). When a view has no operator-saved
# canvas-state file yet, the SurfaceStore seeds these descriptors so a first
# visit looks familiar; the operator's edits then land in the per-view file.
DEFAULTS_DIR = Path(__file__).resolve().parent / "defaults"


def canvas_state_dir() -> Path:
    """Canvas-state dir under the operator's workspace, resolved at call
    time via `tesseract.paths.workspace_dir()` (used by tests pointing
    at a tmp_path via a `TESSERACT_HOME` override)."""
    return workspace_dir() / "canvas-state"


def safe_view(view: str) -> str | None:
    """Return the view name if it passes the strict charset, else None.
    Closes the path-traversal surface on the ``{view}`` path segment."""
    return view if _VIEW_RE.fullmatch(view) else None


def read_view_blob(view: str) -> dict[str, Any] | None:
    """Return the saved canvas-state blob for a view, or None if absent /
    unreadable / malformed."""
    path = canvas_state_dir() / f"{view}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("canvas_state: read failed for %s: %s", view, exc)
        return None
    return data if isinstance(data, dict) else None


def write_view_blob(view: str, blob: dict[str, Any]) -> None:
    """Atomically write the full canvas-state blob for a view."""
    target_dir = canvas_state_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    final = target_dir / f"{view}.json"
    tmp = target_dir / f"{view}.tmp.json"
    tmp.write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, final)


def read_default_layout(view: str) -> list[dict[str, Any]] | None:
    """Return the source-controlled baseline ``surfaces`` for a view, or None
    if no baseline ships. Used to seed a familiar first-visit layout before
    the operator has saved any canvas-state of their own (Y-3)."""
    safe = safe_view(view)
    if safe is None:
        return None
    path = DEFAULTS_DIR / f"{safe}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("canvas_state: default layout read failed for %s: %s", view, exc)
        return None
    surfaces = data.get("surfaces") if isinstance(data, dict) else None
    return surfaces if isinstance(surfaces, list) else None


def persist_surfaces(view: str, surfaces: list[dict[str, Any]]) -> None:
    """Write ``surfaces`` into the view's canvas-state file, preserving the
    frontend-owned ``tldraw_snapshot`` + ``viewport`` keys."""
    blob = read_view_blob(view) or {
        "schema_version": SCHEMA_VERSION,
        "view": view,
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "tldraw_snapshot": None,
    }
    blob["schema_version"] = SCHEMA_VERSION
    blob["view"] = view
    blob["surfaces"] = surfaces
    blob["saved_at_utc"] = datetime.now(timezone.utc).isoformat()
    write_view_blob(view, blob)


__all__ = [
    "DEFAULTS_DIR",
    "SCHEMA_VERSION",
    "canvas_state_dir",
    "persist_surfaces",
    "read_default_layout",
    "read_view_blob",
    "safe_view",
    "write_view_blob",
]
