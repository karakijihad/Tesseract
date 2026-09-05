"""TokenJuice audit log — JSONL append; one line per process() call.

Resolves TESSERACT_HOME at call time so monkeypatched tests stay isolated
from the production logs tree, which they may never write to.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tesseract.kernel.adapters._estimate import tokens_from_chars
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
    """Estimated token count, through the runtime's one measured divisor.

    Telemetry rather than budgeting, but it reads the same seam the budget
    does: an audit that scores a compression against a different idea of a
    token is scoring something nobody else can act on.
    """
    return tokens_from_chars(len(text))
