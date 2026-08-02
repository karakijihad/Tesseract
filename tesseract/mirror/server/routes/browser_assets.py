"""GET /api/browser-assets/{cid}/{seq} — serve P4-2 browser screenshots
written by BrowserManager under <TESSERACT_HOME>/browser/{cid}/{seq}.
Segment-validated; mirrors routes/downloads.py's safe-segment guard."""

from __future__ import annotations

import os
import re
from pathlib import Path

from aiohttp import web

_SAFE_SEG = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")


def _browser_root() -> Path:
    # Browser captures are machine-local, so they live under the `runtime/`
    # sibling — not in the operator's home, which syncs between PCs.
    from tesseract.paths import runtime_dir

    return runtime_dir() / "browser"


async def get_browser_asset(request: web.Request) -> web.Response:
    cid = request.match_info["cid"]
    seq = request.match_info["seq"]
    if not _SAFE_SEG.match(cid) or not _SAFE_SEG.match(seq):
        return web.json_response({"error": "not_found"}, status=404)
    path = (_browser_root() / cid / seq).resolve()
    root = _browser_root().resolve()
    # path-traversal guard: resolved path must stay under the browser root.
    if root not in path.parents or not path.is_file():
        return web.json_response({"error": "not_found"}, status=404)
    return web.FileResponse(path, headers={"Content-Type": "image/png"})


def register(app: web.Application) -> None:
    app.router.add_get("/api/browser-assets/{cid}/{seq}", get_browser_asset)
