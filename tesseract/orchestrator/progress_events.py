"""Long-task progress events — canonical schema + emit helper.

Long-running operations publish ``ProgressEvent`` to
``BackgroundEventBus``. Mirror's WebSocket forwarder
(``mirror/server/ws.py``) subscribes to the bus and relays events to
connected clients automatically.

Live emission sites:
- ``brain/dreaming.py``                     (DreamingEngine cycles)
- ``memory/retrieval.py``                   (config-gated retrieval)

Future emission sites:
- delegated worker runs (coder_seat / auditor_seat / markdown agent)

Config: there is no live orchestrator config file today; ``emit()``
takes an optional ``progress_events`` dict from callers that have one
and falls back to ``enabled=True, emit_retrieval=False`` otherwise.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

logger = logging.getLogger(__name__)

RunType = Literal["dream_task", "multiagent_run", "retrieval", "compaction"]
StepStatus = Literal["started", "in_progress", "done", "failed"]


@dataclass
class ProgressEvent:
    run_id: str
    run_type: RunType
    step_index: int
    label: str
    status: StepStatus
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    step_total: int | None = None
    ended_at: datetime | None = None
    detail: str | None = None
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "run_type": self.run_type,
            "step_index": self.step_index,
            "step_total": self.step_total,
            "label": self.label,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "detail": self.detail,
        }


def emit(event: ProgressEvent, config: dict | None = None) -> None:
    """Publish a ProgressEvent to the BackgroundEventBus.

    config: optional ``progress_events`` sub-dict (no live config file
    today — passed in by callers that have one). When None, defaults to
    enabled=True, emit_retrieval=False. Retrieval events are suppressed
    unless emit_retrieval=True.
    """
    cfg = config or {}
    if not cfg.get("enabled", True):
        return
    if event.run_type == "retrieval" and not cfg.get("emit_retrieval", False):
        return

    try:
        from tesseract.orchestrator.background_event_bus import get_background_bus
        get_background_bus().publish("progress_event", event.to_dict())
    except Exception:
        logger.debug("ProgressEvent publish failed (bus unavailable)", exc_info=True)
