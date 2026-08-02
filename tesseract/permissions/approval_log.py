"""Durable approval ledger — append-only JSONL of every permission decision.

Audit-4 P1 fix: Mirror's `event_log.py` is an in-memory deque (cap 5000)
and the REPL has no record at all. Once TARS commands terminals,
subprocesses, and delegated workers, ASK history must survive process
restarts for forensics and ops review.

Lives at ``runtime/logs/approvals.jsonl`` (``runtime_logs_root()``). One JSON object per
line. Single async lock serializes writes so concurrent tool calls
cannot interleave bytes.

Schema (per line)::

    {
      "ts": ISO8601 with timezone,
      "session_id": str,
      "call_id": str,
      "tool": str,
      "input_summary": dict (truncated for large fields),
      "posture_source": "security" | "path_validator" | "path" |
                        "mode" | "default" | "tool" |
                        "workspace_decision" |
                        "tool_tier_promotion" | "pty_delegate" |
                        "channel_mutation" | "installed_tree",
      "result": "allow_once" | "deny" | "timeout",
      "actor": "operator" | "timeout" | "system"
    }

The ``input_summary`` is ``BaseModel.model_dump()`` with any string field
longer than ``_MAX_SUMMARY_FIELD_CHARS`` truncated to that length plus a
``"...<truncated N chars>"`` suffix. Keeps the ledger scannable without
losing the leading bytes that identify the request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from tesseract.paths import TESSERACT_HOME, runtime_logs_root

logger = logging.getLogger(__name__)

# threading.Lock, not asyncio.Lock: `_append` runs on worker threads via
# `asyncio.to_thread`, and a thread lock serializes appends across every
# event loop in the process — an import-time asyncio.Lock binds to the
# first loop that awaits it and raises from any other (Deferred 2026-07-12).
_LOCK = threading.Lock()
_MAX_SUMMARY_FIELD_CHARS = 500

PostureSource = Literal[
    "security",
    "path_validator",
    "path",
    "mode",
    "default",
    "tool",
    "workspace_decision",
    "tool_tier_promotion",
    "pty_delegate",
    "channel_mutation",
    "installed_tree",
]
Result = Literal["allow_once", "deny", "timeout", "cancelled", "resolved", "deleted"]
Actor = Literal["operator", "timeout", "system"]


def _truncate_field(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_SUMMARY_FIELD_CHARS:
        return (
            value[:_MAX_SUMMARY_FIELD_CHARS]
            + f"...<truncated {len(value) - _MAX_SUMMARY_FIELD_CHARS} chars>"
        )
    if isinstance(value, dict):
        return {k: _truncate_field(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate_field(v) for v in value]
    return value


def summarize_input(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize a tool input dict for the ledger. Truncates long strings."""
    if not raw:
        return {}
    return {k: _truncate_field(v) for k, v in raw.items()}


def ledger_path() -> Path:
    """Resolve the current ledger path. Read at call time so test fixtures
    that override ``TESSERACT_HOME`` via env-var continue to work after
    this module has already been imported."""
    override = os.environ.get("TESSERACT_HOME")
    home = Path(override).resolve() if override else TESSERACT_HOME
    return runtime_logs_root() / "approvals.jsonl"


async def record_ask(
    *,
    session_id: str,
    call_id: str,
    tool_name: str,
    input_summary: dict[str, Any],
    posture_source: PostureSource,
    result: Result,
    actor: Actor,
) -> None:
    """Append one decision row to ``approvals.jsonl``. Best-effort: I/O
    failures are logged but never raised — a broken disk must not block a
    tool decision that's already been made."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "call_id": call_id,
        "tool": tool_name,
        "input_summary": input_summary,
        "posture_source": posture_source,
        "result": result,
        "actor": actor,
    }
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    path = ledger_path()
    try:
        await asyncio.to_thread(_append, path, line)
    except OSError as exc:
        logger.warning("approval_log: append failed for %s: %s", path, exc)


def _append(path: Path, line: str) -> None:
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
