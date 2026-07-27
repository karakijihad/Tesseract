"""Workspace events / comments — Phase 1 of the Workspace tab.

`workspace_events` is the python module name; `tesseract/workspace/` on
disk is the operator-private gitignored markdown directory (SOUL.md,
diary, identity). Naming kept distinct so neither name shadows the other.
"""

from tesseract.workspace_events.events import (
    EventStore,
    WorkspaceComment,
    WorkspaceEvent,
)

__all__ = ["EventStore", "WorkspaceComment", "WorkspaceEvent"]
