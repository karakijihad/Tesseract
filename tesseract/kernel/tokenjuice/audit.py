"""TokenJuice audit log — JSONL append; one line per process() call.

Resolves TESSERACT_HOME at call time so monkeypatched tests stay isolated
from the production logs tree (CLAUDE.md hard rule §logs).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tesseract.paths import log_dir


def audit_dir() -> Path:
    return log_dir("tokenjuice")


def write_audit(record: dict[str, Any]) -> None:
    """Append a single JSONL record. Best-effort — caller handles failures."""
    d = audit_dir()
    d.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
    # newline="" disables Windows \n→\r\n translation; JSONL readers split on \n.
    with (d / "audit.jsonl").open("a", encoding="utf-8", newline="") as f:
        f.write(line)


def count_tokens(text: str) -> int:
    """Heuristic token count — ~4 chars per token (OpenAI rule of thumb).

    Used only for audit telemetry; not for budgeting. Real model token
    counts come from each adapter's tokenizer.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)
