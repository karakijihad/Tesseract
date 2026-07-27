"""Mirror backend shutdown-intent helper.

Single home for the "write ``intent.json`` before exit" logic so every
clean-shutdown path goes through the same call. AU-1 wires this from
``_on_shutdown`` (signal-driven, SIGTERM/SIGINT from the supervisor or
operator terminal). AU-1 S2 will add a ``POST /api/runtime/shutdown``
route that goes through the same helper before triggering the aiohttp
shutdown.

The intent file is the only signal the supervisor has for "this was
orderly, not a crash" — without this hook, every shutdown looks like a
crash and the supervisor would respawn forever after operator_quit.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import tesseract.paths
from tesseract.supervisor.intent import (
    IntentFile,
    IntentKind,
    IntentSource,
    intent_path,
    now_utc,
    write_atomic,
)

log = logging.getLogger(__name__)


def _resolve_home() -> Path:
    """Resolve ``TESSERACT_HOME`` at call time so test fixtures that
    set the env var BEFORE invoking lifecycle helpers don't need to
    reload this module. Matches the pattern called out in CLAUDE.md
    for log/runtime writers."""
    env = os.environ.get("TESSERACT_HOME")
    if env:
        return Path(env).resolve()
    return tesseract.paths.TESSERACT_HOME


def _has_fresh_route_intent(home: Path) -> bool:
    """True iff ``intent.json`` already names *this* backend PID.

    Route handlers (``/api/runtime/shutdown``,
    ``/api/runtime/restart_for_code_drift``) write the intent BEFORE
    triggering ``loop.stop``, so by the time ``on_aiohttp_shutdown``
    runs the route's intent is on disk for our own PID. The shutdown
    hook must not overwrite a same-PID intent — the restart path
    would degrade to ``operator_quit`` and the supervisor would refuse
    to respawn.
    """
    path = intent_path(home)
    if not path.exists():
        return False
    try:
        import json as _json
        payload = _json.loads(path.read_text(encoding="utf-8"))
        existing = IntentFile.from_payload(payload)
    except Exception:  # noqa: BLE001 — malformed file, treat as absent
        return False
    return existing.backend_pid == os.getpid()


def write_shutdown_intent(
    *,
    intent: IntentKind = "operator_quit",
    source: IntentSource = "backend_signal",
    continuation_id: str | None = None,
    reason: str = "",
    tesseract_home: Path | None = None,
) -> None:
    """Atomic write of ``<TESSERACT_HOME>/runtime/intent.json``.

    Fail-soft: any write error is logged but never raised — the
    supervisor will treat an absent or malformed intent file as
    ``crash`` and respawn the backend, which is the wrong route for an
    operator_quit but far better than an exception cascading through
    the aiohttp shutdown sequence.

    The reverse mistake (file written for a crash because cleanup code
    runs in a finally block on the way down) is avoided by routing
    operator-initiated shutdowns through this helper explicitly —
    background-task cleanup code is never the caller.
    """
    home = tesseract_home or _resolve_home()
    try:
        record = IntentFile(
            intent=intent,
            timestamp=now_utc(),
            source=source,
            continuation_id=continuation_id,
            reason=reason,
            backend_pid=os.getpid(),
        )
        write_atomic(intent_path(home), record)
        log.info(
            "lifecycle: wrote shutdown intent (intent=%s source=%s pid=%s)",
            intent, source, os.getpid(),
        )
    except Exception:
        log.exception("lifecycle: failed to write shutdown intent")


def on_aiohttp_shutdown(app: Any) -> None:
    """Adapter for the aiohttp ``on_shutdown`` hook.

    Called when aiohttp receives SIGINT/SIGTERM, OR after a route
    triggers ``loop.stop`` (the restart-for-code-drift and shutdown
    routes do exactly that). If a route already wrote the intent for
    *this* PID, leave it — overwriting would flip a ``restart_upgrade``
    to ``operator_quit`` and the supervisor would refuse to respawn.

    The signal path (SIGTERM from the supervisor, SIGINT from a
    terminal) leaves no prior intent, so ``operator_quit`` +
    ``backend_signal`` is still the right default.
    """
    if _has_fresh_route_intent(_resolve_home()):
        log.info("lifecycle: on_shutdown — preserving route-written intent")
        return
    write_shutdown_intent(
        intent="operator_quit",
        source="backend_signal",
        reason="aiohttp on_shutdown fired",
    )


__all__ = ["write_shutdown_intent", "on_aiohttp_shutdown"]
