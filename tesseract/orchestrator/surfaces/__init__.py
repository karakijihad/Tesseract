"""Surface Protocol v1 — backend layer.

Tools emit Surface descriptors; the canvas renders them. This package owns
the server-side authority for the ``surfaces`` array inside each view's
canvas-state file (``_shared/surface-protocol.md §Persistence``):

- ``descriptor`` — the Pydantic descriptor model + schema-version guard.
- ``events`` — the ``surface`` background-bus channel + publish helpers.
- ``persistence`` — call-time-resolved canvas-state path + merge-preserving
  read/write so the frontend's tldraw snapshot and the backend's surfaces
  never clobber each other (they own disjoint keys of the same file).
- ``store`` — the process-wide ``SurfaceStore`` singleton the ``surface_*``
  kernel tools and the Mirror REST/WS routes share.
"""

from __future__ import annotations

from tesseract.orchestrator.surfaces.descriptor import (
    SCHEMA_VERSION,
    SurfaceDescriptor,
    SurfacePosition,
    SurfaceSize,
)
from tesseract.orchestrator.surfaces.events import CHANNEL, publish_surface_event
from tesseract.orchestrator.surfaces.store import (
    SurfaceStore,
    get_surface_store,
    reset_surface_store,
)

__all__ = [
    "CHANNEL",
    "SCHEMA_VERSION",
    "SurfaceDescriptor",
    "SurfacePosition",
    "SurfaceSize",
    "SurfaceStore",
    "get_surface_store",
    "publish_surface_event",
    "reset_surface_store",
]
