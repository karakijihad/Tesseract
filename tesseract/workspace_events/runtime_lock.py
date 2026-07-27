"""SU-1 / SU-5 — `runtime_lock_deny` workspace event emitter.

Sync best-effort append. Both call sites (`file_write` lockdown and
`bash` security check #25) are themselves sync paths invoked from
`check_permissions` / `run`, so the emitter cannot await the async
WS broadcast. The disk append is enough — the Mirror inbox repolls
within seconds and surfaces the row.

Failure is always swallowed — emission must never fail the originating
DENY decision.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from tesseract.workspace_events.events import EventStore, WorkspaceEvent

log = logging.getLogger(__name__)


def _logs_dir() -> Path:
    override = os.environ.get("TESSERACT_HOME")
    if override:
        return Path(override).resolve() / "logs"
    from tesseract.paths import TESSERACT_HOME
    return TESSERACT_HOME / "logs"


def emit_runtime_lock_deny(
    *,
    tool: str,
    locked_path: str,
    reason: str,
    command_excerpt: str | None = None,
    check_id: str | None = None,
) -> None:
    """Append one `runtime_lock_deny` event to the workspace store.

    Sync. Best-effort. Never raises.

    `tool` is the originating tool name (`"file_write"` / `"bash"`).
    `locked_path` is the path the DENY fired on. `reason` is the deny
    message returned to the caller. `command_excerpt` is the first 200
    bytes of the bash command when emitted from `bash_tool`; omit for
    `file_write`. `check_id` is the bash_security check number when
    emitted from `bash_tool` (e.g. `"25"`).
    """
    try:
        store = EventStore(_logs_dir())
        payload: dict[str, object] = {
            "tool": tool,
            "locked_path": locked_path,
            "reason": reason,
        }
        if command_excerpt is not None:
            payload["command_excerpt"] = command_excerpt[:200]
        if check_id is not None:
            payload["check_id"] = check_id
        event = WorkspaceEvent.new(
            kind="runtime_lock_deny",
            source="security",
            title=f"runtime lockdown: {tool} → {locked_path}",
            summary=reason,
            payload=payload,
            priority=7,
            author_id="system",
            author_display="Security",
        )
        store.append_event(event)
    except Exception:
        log.exception(
            "runtime_lock_deny: emission failed for tool=%s path=%s "
            "(non-fatal, DENY decision still applies)",
            tool, locked_path,
        )


__all__ = ["emit_runtime_lock_deny"]
