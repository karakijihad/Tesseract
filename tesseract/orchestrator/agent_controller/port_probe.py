"""Controller port-liveness probe.

Extracted from ``daemon.py`` (lane-cleanup Batch 4). The autonomy kernel's
dynamic kind resolver calls :func:`controller_port_alive` to decide whether to
route an ``OPERATOR_GATE`` item to the live controller or fall back to
``CODER_SEAT``. The probe is TTL-cached so a single tick that admits N items
pays the TCP-connect cost at most once.
"""

from __future__ import annotations

from .paths import port_file_path

_PORT_ALIVE_CACHE: dict[str, tuple[float, bool]] = {}
"""TTL cache for :func:`controller_port_alive` keyed by port-file path.

The autonomy kernel calls the probe up to ``top_k`` times per tick from
the event-loop thread; a 0.5 s socket connect is enough to stall WS
heartbeats and inbound chat turns. Caching for ~15 s keeps the dispatch
decision fresh while pinning the worst-case event-loop stall to one
probe per cache miss.
"""

_PORT_ALIVE_TTL_SECONDS: float = 15.0


def _do_port_probe(timeout: float) -> bool:
    import socket

    path = port_file_path()
    if not path.exists():
        return False
    try:
        port = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if port <= 0 or port > 65535:
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


def controller_port_alive(timeout: float = 0.5) -> bool:
    """Cheap probe used by the autonomy kernel's dynamic kind resolver. True
    if `<TESSERACT_HOME>/run/controller.port` exists AND a TCP connection
    to that port on 127.0.0.1 succeeds within `timeout` seconds.

    Result is cached for :data:`_PORT_ALIVE_TTL_SECONDS` keyed by the
    resolved port-file path so a single autonomy tick that admits N items
    pays the connect cost at most once. Failure modes (missing port file,
    unparseable port, refused connect, timeout) all collapse to False;
    the kernel falls back to CODER_SEAT.
    """
    import time as _time

    key = str(port_file_path())
    cached = _PORT_ALIVE_CACHE.get(key)
    now = _time.monotonic()
    if cached is not None and now - cached[0] < _PORT_ALIVE_TTL_SECONDS:
        return cached[1]
    alive = _do_port_probe(timeout)
    _PORT_ALIVE_CACHE[key] = (now, alive)
    return alive


def reset_port_alive_cache() -> None:
    """Test hook — flush the TTL cache between cases so a monkeypatched
    probe takes effect on the next call."""
    _PORT_ALIVE_CACHE.clear()


__all__ = ["controller_port_alive", "reset_port_alive_cache"]
