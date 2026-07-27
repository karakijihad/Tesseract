"""P4-2 — PC/browser tool audit sink: one JSON line per Tier-2 call to
``<TESSERACT_HOME>/logs/audit/pc.jsonl``. Path resolved at call time,
async, lock-serialized, best-effort."""

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


def pc_audit_path() -> Path:
    override = os.environ.get("TESSERACT_HOME")
    from tesseract.paths import TESSERACT_HOME
    home = Path(override).resolve() if override else TESSERACT_HOME
    return home / "logs" / "audit" / "pc.jsonl"


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)


async def append_pc_audit_row(
    *,
    tool: str,
    input: dict[str, Any],
    posture: str,
    decision: str = "approved",
    result_summary: str = "",
    session_id: str = "",
) -> None:
    entry = {
        "at_utc": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "input": input,
        "posture": posture,
        "decision": decision,
        "result_summary": result_summary,
        "session_id": session_id,
    }
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    path = pc_audit_path()
    try:
        async with _LOCK:
            await asyncio.to_thread(_append, path, line)
    except OSError as exc:
        log.warning("pc_audit: append failed for %s: %s", path, exc)


__all__ = ["append_pc_audit_row", "pc_audit_path"]
