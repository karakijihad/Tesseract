"""GET /api/conscience/drift — latest drift report + short history.

Scrapes `tesseract/logs/conscience/drift-*.jsonl`, returns the most
recent report line plus up to 30 prior reports (for the ConscienceView
sparkline). Responds 200 with `{"report": null, "history": []}` when
no reports have been written yet — the frontend renders a
heartbeat-disabled empty state rather than an error.
"""

from __future__ import annotations

import json
from pathlib import Path

from aiohttp import web

from tesseract.paths import log_dir


def _drift_dir() -> Path:
    """Conscience drift follows the operator, so it lives under `home/logs`.
    Call-time: an import-time constant freezes the path."""
    return log_dir("conscience")

_HISTORY_LIMIT = 30


async def drift(request: web.Request) -> web.Response:
    drift_dir = _drift_dir()
    files = sorted(drift_dir.glob("drift-*.jsonl")) if drift_dir.exists() else []
    if not files:
        return web.json_response({"report": None, "history": []})
    latest = _load_last_report(files[-1])
    history = [r for r in (_load_last_report(f) for f in files[-_HISTORY_LIMIT:]) if r]
    return web.json_response({"report": latest, "history": history})


def _load_last_report(path: Path) -> dict | None:
    last: dict | None = None
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                last = json.loads(raw)
            except json.JSONDecodeError:
                continue
    return last
