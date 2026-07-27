"""Minimal aiohttp backend used by the AU-1 smoke harness.

Stand-in for the real Mirror: just exposes ``/api/health`` and writes
``intent.json {operator_quit, source: backend_signal}`` on SIGINT /
SIGTERM / CTRL_BREAK_EVENT before exit. Reads port from
``$SMOKE_PORT``; writes intent under ``$TESSERACT_HOME/runtime/``.

Lives under ``tests/smoke/`` so pytest doesn't collect it.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web


def _write_intent(home: Path) -> None:
    runtime = home / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    payload = {
        "intent": "operator_quit",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "backend_signal",
        "backend_pid": os.getpid(),
        "reason": "smoke backend received stop signal",
    }
    path = runtime / "intent.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "uptime_seconds": time.monotonic() - START})


async def shutdown_handler(app: web.Application) -> None:
    print(f"smoke_backend[{os.getpid()}]: on_shutdown — writing intent.json", flush=True)
    _write_intent(Path(os.environ["TESSERACT_HOME"]))


def _install_windows_break_handler() -> None:
    if sys.platform != "win32" or not hasattr(signal, "SIGBREAK"):
        return
    def _on_break(_signum, _frame):  # type: ignore[no-untyped-def]
        try:
            signal.raise_signal(signal.SIGINT)
        except Exception:
            raise KeyboardInterrupt()
    signal.signal(signal.SIGBREAK, _on_break)  # type: ignore[attr-defined]


def _watch_stop_request() -> None:
    """Polls for the supervisor's stop_request file. Matches the real
    Mirror's watcher so smoke_backend gets the same cross-console
    shutdown path."""
    import threading
    import time
    from pathlib import Path

    def _loop() -> None:
        home = Path(os.environ["TESSERACT_HOME"])
        path = home / "runtime" / "stop_request"
        while True:
            try:
                if path.exists():
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    try:
                        signal.raise_signal(signal.SIGINT)
                    except Exception:
                        os._exit(0)
                    return
            except OSError:
                pass
            time.sleep(0.5)

    threading.Thread(target=_loop, name="stop-request-watcher", daemon=True).start()


def main() -> int:
    _install_windows_break_handler()
    _watch_stop_request()
    port = int(os.environ.get("SMOKE_PORT", "8765"))
    app = web.Application()
    app.router.add_get("/api/health", health)
    app.on_shutdown.append(shutdown_handler)
    print(f"smoke_backend[{os.getpid()}]: listening on 127.0.0.1:{port}", flush=True)
    web.run_app(app, host="127.0.0.1", port=port, print=None)
    print(f"smoke_backend[{os.getpid()}]: exit 0", flush=True)
    return 0


START = time.monotonic()


if __name__ == "__main__":
    sys.exit(main())
