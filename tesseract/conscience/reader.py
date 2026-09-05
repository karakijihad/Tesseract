"""Shared reader for drift-*.jsonl reports.

Both `ConscienceHeartbeatJob` (for transition detection) and
`ConscienceStatusTool` (for on-demand inspection) need to load the most
recent report line. Keeping the reader here avoids circular imports —
the tool module can't pull from `brain.boot` and the scheduler module
shouldn't duplicate the loader byte-for-byte.

The tallies are here for the same reason, and for a worse one: they moved from
`summary` to `counts` when every record was given one shape, `summary` became
the sentence a person reads, and three readers had to learn that. Two did. The
third assembles the system prompt, so on the first record written in the new
shape it raised `AttributeError` on a string, chat infra came up unavailable,
and the app looked disconnected. One reader, and that cannot happen twice.
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


def report_counts(report: dict | None) -> dict[str, int]:
    """The ok/warn/bad tallies from one report, whatever shape wrote it.

    `counts` since every record was given one shape; `summary` on the rows
    written before that, which are still on disk and still read. `{}` for
    anything else, including a report whose `summary` is the sentence.
    """
    if not isinstance(report, dict):
        return {}
    for key in ("counts", "summary"):
        value = report.get(key)
        if isinstance(value, dict):
            return value
    return {}
