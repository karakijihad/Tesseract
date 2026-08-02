"""GET /api/home/{tree}/{path} — read-only serving of the operator's files.

Mirror surfaces render URLs, not `file://` paths, so a PDF sitting in
``home/downloads/`` had no way to reach a surface at all. This serves the
three trees whose contents ARE surface material.

`config/`, `.env`, `memory-store/` and `sessions/` are deliberately absent:
they are not surface content, and exposing them would widen what a local
HTTP port reaches for no gain.

Segment-validated, then re-checked after `resolve()` — mirrors
`routes/browser_assets.py`, extended to multi-segment paths and to symlinks
that point out of the tree.
"""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path

from aiohttp import web

_SERVED_TREES = frozenset({"downloads", "vault", "tars-workshop"})
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_.\-][A-Za-z0-9_.\- ]{0,127}$")
_DEFAULT_TYPE = "application/octet-stream"

# Only these render inline. Everything else downloads.
#
# `downloads/` holds files TARS fetched from the web, so its contents are not
# trusted input. Serving an .html or .svg inline would run its script on the
# Mirror's own origin, with same-origin reach into every other API on this
# port — the tree exists to show the operator a PDF, not to host active
# content.
_INLINE_TYPES = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "text/plain",
    }
)


def _resolve_within(tree: str, relative: str) -> Path | None:
    """Return the file `relative` names inside `tree`, or None if anything
    about the request is off. None always becomes a 404 — never a message
    that distinguishes "blocked" from "absent"."""
    from tesseract.paths import home_dir

    segments = [segment for segment in relative.split("/") if segment]
    if not segments:
        return None
    for segment in segments:
        if segment in (".", "..") or not _SAFE_SEGMENT.match(segment):
            return None

    root = (home_dir() / tree).resolve()
    candidate = root.joinpath(*segments).resolve()
    # Re-check AFTER resolve(): a symlink inside the tree can still land
    # outside it, and a string comparison on the URL would not notice.
    if root not in candidate.parents or not candidate.is_file():
        return None
    return candidate


async def get_home_file(request: web.Request) -> web.Response:
    tree = request.match_info["tree"]
    if tree not in _SERVED_TREES:
        return web.json_response({"error": "not_found"}, status=404)

    path = _resolve_within(tree, request.match_info.get("path", ""))
    if path is None:
        return web.json_response({"error": "not_found"}, status=404)

    guessed, _ = mimetypes.guess_type(path.name)
    content_type = guessed or _DEFAULT_TYPE
    inline = content_type in _INLINE_TYPES

    return web.FileResponse(
        path,
        headers={
            "Content-Type": content_type,
            "Content-Disposition": (
                "inline" if inline else f'attachment; filename="{path.name}"'
            ),
            # The declared type is the only one honoured — without this a
            # browser may sniff an .png of HTML back into markup.
            "X-Content-Type-Options": "nosniff",
            # Defence in depth for the inline set: nothing it loads may reach
            # the network or this origin's scripts.
            "Content-Security-Policy": "sandbox; default-src 'none'; object-src 'none'",
        },
    )


def register(app: web.Application) -> None:
    app.router.add_get("/api/home/{tree}/{path:.*}", get_home_file)
