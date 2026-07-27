"""AS-1 — Unified Activity Registry.

The single backend index of everything TARS is running (delegates, lanes,
controller sessions; AS-3 adds routines). A derived
projection over the substrates' canonical on-disk state, published on the
``activity`` bus channel for the Mirror to reflect. See
``Docs/Plan/tars-cockpit/INDEX.md`` (AS-arc).
"""

from tesseract.orchestrator.activity.events import (
    CHANNEL,
    make_activity_envelope,
    publish_activity_event,
)
from tesseract.orchestrator.activity.models import (
    ActivityKind,
    ActivityRecord,
    ActivityRecordOut,
    ActivityState,
    DurabilityClass,
)
from tesseract.orchestrator.activity.registry import (
    ActivityRegistry,
    get_activity_registry,
    reset_activity_registry,
)

__all__ = [
    "CHANNEL",
    "make_activity_envelope",
    "publish_activity_event",
    "ActivityKind",
    "ActivityRecord",
    "ActivityRecordOut",
    "ActivityState",
    "DurabilityClass",
    "ActivityRegistry",
    "get_activity_registry",
    "reset_activity_registry",
]
