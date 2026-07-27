"""Append-only refresh log for KB subdirs.

Per :file:`_shared/knowledge-base-layout.md`, each subdir keeps a
``_refresh-log.jsonl`` of ``{ts, file, diff_summary}`` rows. Operator
greps these to spot drift / patterns; tooling tails the last N rows for
debug surfaces.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()


def append_refresh_row(
    subdir: Path,
    *,
    file: str,
    diff_summary: str,
    ts: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Append a single JSONL row to ``<subdir>/_refresh-log.jsonl``.

    Returns the log path. Creates the file if missing. Atomic at the
    line level — file is opened with append mode, single write.
    """
    log_path = Path(subdir) / "_refresh-log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    row: dict[str, Any] = {
        "ts": ts or datetime.now(timezone.utc).isoformat(),
        "file": file,
        "diff_summary": diff_summary,
    }
    if extra:
        row.update(extra)
    payload = json.dumps(row, ensure_ascii=False) + "\n"
    with _LOCK:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(payload)
    return log_path
