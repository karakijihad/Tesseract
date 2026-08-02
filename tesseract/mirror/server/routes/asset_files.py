"""GET /api/asset?path=&sig= — serve one file that `open` signed.

Unlike `routes/home_files.py`, which serves three whole trees, this endpoint
has no ambient reach: a request without a valid signature is a 404, so the
served set is exactly the files the operator opened. That is what lets a canvas
card point at a file living beside `.env` without exposing the neighbourhood.

The signature is checked first, then the read boundary is re-validated anyway.
A signature proves `open` resolved this path once; it does not prove the path
is still inside the boundary, and a boundary that moved should win.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from aiohttp import web

from tesseract.orchestrator.open_verb.asset_token import verify
from tesseract.orchestrator.open_verb.suffixes import is_secret
from tesseract.paths import home_dir, install_root
from tesseract.permissions.path_validator import validate_path

_DEFAULT_TYPE = "application/octet-stream"

# Rendered in place. Deliberately excludes `text/html` and `image/svg+xml`:
# both execute script, and a document opened from an untrusted source would run
# it on the Mirror's own origin with same-origin reach into every other API on
# this port. Local HTML reaches the canvas as an `html` surface carrying text,
# never through this endpoint.
_INLINE_TYPES = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/bmp",
        "text/plain",
        "video/mp4",
        "video/webm",
        "video/ogg",
        "audio/mpeg",
        "audio/wav",
        "audio/ogg",
        "audio/flac",
        "audio/mp4",
    }
)


def _authorized(raw_path: str, signature: str) -> Path | None:
    """None always becomes a 404 — the response never distinguishes a bad
    signature from a missing file, so this endpoint cannot be used to probe
    which paths exist."""
    if not raw_path or not verify(raw_path, signature):
        return None

    ok, _reason = validate_path(
        raw_path,
        write_root=str(home_dir()),
        read_root=str(install_root()),
        mode="read",
        resolve_symlinks=True,
    )
    if not ok:
        return None

    candidate = Path(raw_path).expanduser().resolve()

    # Enforced HERE, not only in the resolver. A signature is durable: one
    # minted before this rule existed, or by any future caller that forgets it,
    # would otherwise still serve. The endpoint is the last gate before bytes
    # leave, so it does not delegate this to whoever produced the link.
    #
    # Credentials only. The operator's own memory, sessions and journal are
    # theirs to look at — this endpoint needs a signature, so nothing reaches it
    # that `open` did not resolve on their behalf.
    if is_secret(candidate.name):
        return None

    if not candidate.is_file():
        return None
    return candidate


async def get_asset(request: web.Request) -> web.Response:
    path = _authorized(
        request.query.get("path", ""),
        request.query.get("sig", ""),
    )
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
            # Without this a browser may sniff an .png of markup back into HTML.
            "X-Content-Type-Options": "nosniff",
            # Defence in depth for the inline set: nothing it loads may reach
            # the network or this origin's scripts.
            "Content-Security-Policy": "sandbox; default-src 'none'; object-src 'none'",
        },
    )


def register(app: web.Application) -> None:
    app.router.add_get("/api/asset", get_asset)
