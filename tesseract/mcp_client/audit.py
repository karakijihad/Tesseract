"""Outbound MCP-client audit sink — one JSON line per external tool call to
``<TESSERACT_HOME>/logs/audit/mcp-client.jsonl``.

The inverse of the inbound server sink (``mirror/server/mcp/audit.py``): that
records verbs OTHER clients call on us; this records tool calls WE make on
external servers. Same discipline — path resolved at call time (test-leak-safe),
async, lock-serialized, best-effort — a failed audit write never fails the tool
call. Raw arguments are never logged; only a ``params_hash`` correlates repeats
(P5 no-params-leakage rule, mirrored here).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_LOCK = asyncio.Lock()


def mcp_client_audit_path() -> Path:
    override = os.environ.get("TESSERACT_HOME")
    from tesseract.paths import TESSERACT_HOME, log_dir

    home = Path(override).resolve() if override else TESSERACT_HOME
    return log_dir("audit") / "mcp-client.jsonl"


def hash_params(params: dict) -> str:
    """SHA-256[:16] of the JSON params — correlates repeat calls WITHOUT ever
    logging raw arguments. Order-independent (sorted keys)."""
    blob = json.dumps(params or {}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)


async def append_mcp_client_audit_row(
    *,
    server: str,
    tool: str,
    remote_tool: str,
    outcome: str,
    params_hash: str = "",
    result_summary: str = "",
) -> None:
    """Append one audit row for an executed external MCP tool call.

    Only calls that pass the permission gate reach ``tool.run`` and hence this
    sink; denials/declines are recorded by ``permissions/approval_log`` on the
    decide path. ``outcome`` is one of ``ok`` / ``error`` / ``timeout``.
    """
    entry = {
        "at_utc": datetime.now(timezone.utc).isoformat(),
        "server": server,
        "tool": tool,
        "remote_tool": remote_tool,
        "outcome": outcome,
        "params_hash": params_hash,
        "result_summary": result_summary,
    }
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    path = mcp_client_audit_path()
    try:
        async with _LOCK:
            await asyncio.to_thread(_append, path, line)
    except OSError as exc:
        log.warning("mcp_client_audit: append failed for %s: %s", path, exc)


__all__ = [
    "append_mcp_client_audit_row",
    "hash_params",
    "mcp_client_audit_path",
]
