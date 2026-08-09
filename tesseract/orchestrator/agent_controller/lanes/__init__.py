"""X-4 lane substrate — stream-resumable controller-owned coder/auditor lanes.

`LaneManager` exposes the six-method `lane.*` contract documented in
`Docs/Plan/cockpit/_shared/lane-contract.md`. The substrate composes
existing primitives — `ClaudeStreamAdapter` / `CodexStreamAdapter` for
headless transport. All lane state is file-canonical under
`<TESSERACT_HOME>/controller/lanes/<id>/`; the brain holds no lane state.
P-3 brain-restart invariant: brain reads lane records on boot and calls
`lane.attach` per id.
"""

from __future__ import annotations

from .events_log import (
    LaneEventsCursor,
    append_event,
    read_events_since,
)
from .manager import (
    LaneAdapter,
    LaneManager,
    LaneManagerError,
    LaneNotFoundError,
)
from .models import (
    Lane,
    LaneEvent,
    LaneEventKind,
    LaneKind,
    LaneMode,
    LaneSendResult,
    LaneSnapshot,
    LaneStatus,
)
from .named import (
    InvalidNamedLaneNameError,
    NamedLaneError,
    NamedLaneManager,
    NamedLaneRecord,
    delete_named_lane,
    list_named_lanes,
    named_lanes_root,
    read_named_lane,
    write_named_lane,
)
from .store import (
    archive_lane,
    lane_dir,
    lanes_root,
    list_lane_ids,
    read_lane,
    write_lane,
)

__all__ = [
    "InvalidNamedLaneNameError",
    "Lane",
    "LaneAdapter",
    "LaneEvent",
    "LaneEventKind",
    "LaneEventsCursor",
    "LaneKind",
    "LaneManager",
    "LaneManagerError",
    "LaneMode",
    "LaneNotFoundError",
    "LaneSendResult",
    "LaneSnapshot",
    "LaneStatus",
    "NamedLaneError",
    "NamedLaneManager",
    "NamedLaneRecord",
    "append_event",
    "archive_lane",
    "delete_named_lane",
    "lane_dir",
    "lanes_root",
    "list_lane_ids",
    "list_named_lanes",
    "named_lanes_root",
    "read_events_since",
    "read_lane",
    "read_named_lane",
    "write_lane",
    "write_named_lane",
]
