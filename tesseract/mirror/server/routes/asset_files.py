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
    return _authorize_path(raw_path)


def _authorize_path(raw_path: str) -> Path | None:
    """Everything the signature does not cover: where the path lands right
    now, and whether that is a file we may serve.

    Split out from `_authorized` because it is re-run just before the bytes
    are read. The signature is over the path *string*, which cannot change;
    what the string resolves to can.
    """
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


class _GuardedFileResponse(web.FileResponse):
    """A `FileResponse` that re-checks the path just before it opens it.

    `get_asset` authorizes a path and hands aiohttp a `Path`; aiohttp then
    stats and opens that pathname again, by name, inside `prepare()`. What
    the name resolves to can change in between — swap the file for a link
    at a secret and the checks passed on one file while the bytes come from
    another.

    Re-running the check here moves the gap from a whole handler return
    down to the microseconds inside `prepare()`. It does not close it: this
    is still name-based, so a sufficiently lucky racer wins. Closing it
    properly means serving from a descriptor opened exactly once, which
    means hand-rolling Range and conditional requests that `FileResponse`
    gives us for nothing — and this route is the one that streams video.

    The refusal follows aiohttp's own idiom for an unreadable file: set the
    status and delegate to `StreamResponse.prepare`, skipping the body.
    Raising here would escape — `prepare()` runs inside `finish_response`,
    outside the handler's `HTTPException` guard, so an exception becomes a
    500 or a dropped connection rather than the 404 this owes the caller.
    """

    def __init__(self, path: Path, *, raw_path: str, **kwargs) -> None:
        super().__init__(path, **kwargs)
        self._raw_path = raw_path
        self._authorized_path = path

    async def prepare(self, request: web.BaseRequest):
        if _authorize_path(self._raw_path) != self._authorized_path:
            self.set_status(web.HTTPNotFound.status_code)
            return await super(web.FileResponse, self).prepare(request)
        return await super().prepare(request)


async def get_asset(request: web.Request) -> web.Response:
    raw_path = request.query.get("path", "")
    path = _authorized(raw_path, request.query.get("sig", ""))
    if path is None:
        return web.json_response({"error": "not_found"}, status=404)

    guessed, _ = mimetypes.guess_type(path.name)
    content_type = guessed or _DEFAULT_TYPE
    inline = content_type in _INLINE_TYPES

    return _GuardedFileResponse(
        path,
        raw_path=raw_path,
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
