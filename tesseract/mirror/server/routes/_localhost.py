"""Same-machine check for routes whose blast radius is worse than a read.

Mirror binds 127.0.0.1 only (`config/mirror.yaml::server.host`) and runs
with no auth — "local-only, single-operator" is the whole threat model. So
for most routes the bind IS the gate.

It is not enough on its own for the handful of endpoints that restart the
backend or download and execute a vendor installer. Two reasons: a future
bind change would silently expose them without anyone revisiting auth, and
CORS does not cover this case either — `cors.py::origin_is_allowed` lets a
request with no `Origin` header through by design, because that is what a
native client sends. A check at the handler keeps the decision next to the
thing being protected.
"""

from __future__ import annotations

from aiohttp import web

_LOCALHOST_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def is_localhost_request(request: web.Request) -> bool:
    """True when the request originated from this machine."""
    remote = (request.remote or "").strip()
    return remote in _LOCALHOST_HOSTS
