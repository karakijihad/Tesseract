"""Shared reader for drift-*.jsonl reports.

Both `ConscienceHeartbeatJob` (for transition detection) and
`ConscienceStatusTool` (for on-demand inspection) need to load the most
recent report line. Keeping the reader here avoids circular imports —
the tool module can't pull from `brain.boot` and the scheduler module
shouldn't duplicate the loader byte-for-byte.
"""

from __future__ import annotations

import json
from pathlib import Path


def load_latest_report(target_dir: Path) -> dict | None:
    """Return the most recent report line across all drift-*.jsonl files.

    `None` when the directory doesn't exist, when no drift file is
    present, or when every line is malformed JSON.
    """
    if not target_dir.exists():
        return None
    files = sorted(target_dir.glob("drift-*.jsonl"))
    if not files:
        return None
    last: dict | None = None
    with files[-1].open("r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                last = json.loads(raw)
            except json.JSONDecodeError:
                continue
    return last
