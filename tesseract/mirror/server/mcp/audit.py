"""MCP verb audit sink — one JSON line per verb call to
``<TESSERACT_HOME>/logs/audit/mcp.jsonl``.

A verb *call* is traffic (audit log), not a top-level Activity record
(Doclog 2026-07-01 §No mcp_call). Mirrors ``browser/pc_audit.py``: path
resolved at call time (test-leak-safe), async, lock-serialized, best-effort —
a failed audit write never fails the verb.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_LOCK = asyncio.Lock()


def mcp_audit_path() -> Path:
    override = os.environ.get("TESSERACT_HOME")
    from tesseract.paths import TESSERACT_HOME

    home = Path(override).resolve() if override else TESSERACT_HOME
    return home / "logs" / "audit" / "mcp.jsonl"


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)


async def append_mcp_audit_row(
    *,
    verb: str,
    client: str,
    trust_tier: str,
    posture: str,
    decision: str,
    params_hash: str = "",
    result_summary: str = "",
) -> None:
    entry = {
        "at_utc": datetime.now(timezone.utc).isoformat(),
        "verb": verb,
        "client": client,
        "trust_tier": trust_tier,
        "posture": posture,
        "decision": decision,
        # SHA-256[:16] of the JSON params — correlates repeat calls WITHOUT
        # ever logging raw arguments (P5 no-params-leakage rule).
        "params_hash": params_hash,
        "result_summary": result_summary,
    }
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    path = mcp_audit_path()
    try:
        async with _LOCK:
            await asyncio.to_thread(_append, path, line)
    except OSError as exc:
        log.warning("mcp_audit: append failed for %s: %s", path, exc)


__all__ = ["append_mcp_audit_row", "mcp_audit_path"]
