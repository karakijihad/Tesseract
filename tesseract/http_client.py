"""One TLS trust store for the whole process, and the clients built on it.

Constructing an ``httpx`` client builds an ``ssl.SSLContext`` and loads a CA
bundle into it. Measured on Windows: ``httpx.AsyncClient()`` costs 0.70-0.96s,
``ssl.create_default_context()`` alone is ~0.17s, the bundle load is the rest,
and ``httpx.Client(verify=False)`` is 1ms. The client is not the cost — the
trust store is, and it is identical every time it is built.

It was being built per call at ten sites, and for the async ones **on the
event loop**: the constructor is synchronous CPU work inside an ``async def``,
so three quarters of a second sat between a health check and its deadline
every time the assistant searched the web or rendered a brief. That is the
rule about sync work over 50ms, broken by a line that reads like a
constructor.

So the context is built once and shared. ``ssl.SSLContext`` is designed for
exactly this — a server hands one context to every connection it accepts —
and sharing it changes no verification behaviour: these callers all used the
default trust store, which is what this builds.

Callers use :func:`async_client` / :func:`client` instead of ``httpx``'s
constructors directly. Anything that needs different verification (a pinned
CA, a deliberately unverified probe) passes its own ``verify=`` and opts out,
which is why that stays a normal keyword rather than being taken away.
"""

from __future__ import annotations

import ssl
import threading

import httpx

_CONTEXT: ssl.SSLContext | None = None
# Guards construction only. Without it two threads racing the first call each
# pay the ~0.8s build and one of the two contexts is dropped on the floor.
_CONTEXT_LOCK = threading.Lock()


def ssl_context() -> ssl.SSLContext:
    """The process-wide verification context, built on first use.

    Safe to call from any thread. Callers that can afford to do so should warm
    it off the event loop (``asyncio.to_thread``) before the first request
    needs it — see ``mirror/server/app.py``'s warm-up — so that the one build
    lands somewhere it costs nothing.
    """
    global _CONTEXT
    if _CONTEXT is None:
        with _CONTEXT_LOCK:
            if _CONTEXT is None:
                _CONTEXT = httpx.create_ssl_context()
    return _CONTEXT


def async_client(**kwargs) -> httpx.AsyncClient:
    """``httpx.AsyncClient`` on the shared trust store."""
    kwargs.setdefault("verify", ssl_context())
    return httpx.AsyncClient(**kwargs)


def client(**kwargs) -> httpx.Client:
    """``httpx.Client`` on the shared trust store."""
    kwargs.setdefault("verify", ssl_context())
    return httpx.Client(**kwargs)


__all__ = ["ssl_context", "async_client", "client"]
