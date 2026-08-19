"""Rewrite a video share link into the endpoint that can actually be framed.

Measured, not assumed: `youtube.com/watch?v=<id>` answers with
`X-Frame-Options: SAMEORIGIN` and `youtube.com/embed/<id>` answers with no
framing header at all. Without this rewrite the probe reads the share link as
unframeable and every video the operator asks for opens in an external browser
tab, while the cockpit has had a working player the whole time.

`WebViewRenderer.tsx` carries the same rewrite for URLs that reach a card
without passing through `open`. The two need not accept identical inputs —
what they must agree on is the output, because the renderer grants the media
sandbox (`allow-same-origin`, autoplay, fullscreen) to the embed endpoint
alone.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com"})

# The id is the only thing carried across; everything else in the source URL is
# dropped rather than forwarded, so no path or query can ride the rewrite into
# the elevated sandbox the embed endpoint is granted.
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def embed_url(url: str) -> str | None:
    """The frameable embed endpoint for `url`, or None if it is not one we know."""
    try:
        split = urlsplit(url)
    except ValueError:
        return None

    host = (split.hostname or "").lower()
    if host in _YOUTUBE_HOSTS and split.path == "/watch":
        video_id = parse_qs(split.query).get("v", [""])[0]
    elif host == "youtu.be":
        video_id = split.path.lstrip("/")
    else:
        return None

    if not _VIDEO_ID_RE.match(video_id):
        return None
    return f"https://www.youtube.com/embed/{video_id}"
