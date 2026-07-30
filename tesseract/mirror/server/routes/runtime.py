"""Runtime endpoints — operator-driven backend shutdown + supervisor
visibility.

AU-1 S2:

* ``POST /api/runtime/shutdown`` writes operator_quit intent and
  triggers an aiohttp graceful shutdown. Operator-session-auth-gated;
  anonymous + channel-routed callers are rejected.

* ``GET /api/runtime/status`` exposes supervisor state to the UI.
  Reads ``runtime/intent.json``, ``runtime/crash_storm.json``,
  ``runtime/supervisor.pid`` from disk — no IPC to the supervisor.
  The Runtime panel + header badge poll this every few seconds.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from tesseract.mirror.server.routes.autonomy import filter_live_attention
from tesseract.paths import TESSERACT_HOME
from tesseract.supervisor.breaker import crash_storm_path
from tesseract.supervisor.intent import (
    IntentFile,
    intent_path,
    now_utc,
    runtime_dir,
    write_atomic,
)

log = logging.getLogger(__name__)


def _pid_alive(pid: int) -> bool:
    """Liveness probe routed through the canonical cross-platform helper.

    Windows specifically — ``os.kill(pid, 0)`` returns ``WinError 87``
    ("parameter is incorrect") even for live processes, which we used
    to misclassify as DEAD; this triggered a stale-PID badge in the
    Runtime panel against a perfectly healthy supervisor. The probe
    helper uses ``OpenProcess`` + ``GetExitCodeProcess`` on Windows.
    """
    from tesseract.supervisor.process_probe import pid_alive as _probe
    return _probe(pid)


def _read_supervisor_pid(home: Path) -> tuple[int | None, bool]:
    """Returns (pid, alive). Both None / False when no pid file exists."""
    path = runtime_dir(home) / "supervisor.pid"
    if not path.exists():
        return None, False
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None, False
    return pid, _pid_alive(pid)


def _read_json_or_none(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


async def get_status(request: web.Request) -> web.Response:
    """Snapshot of supervisor + backend state.

    Anonymous-readable on purpose — UI polls this without any session
    context. The data is operator-visible: backend uptime, supervisor
    pid + alive flag, latest intent (if any persisted between routes),
    crash storm marker, AU-2 recovery summary. No secrets.
    """
    home = TESSERACT_HOME
    pid, alive = _read_supervisor_pid(home)
    intent = _read_json_or_none(intent_path(home))
    crash_storm = _read_json_or_none(crash_storm_path(home))

    started = request.app.get("started_at")
    uptime = round(time.monotonic() - started, 3) if started is not None else 0.0

    return web.json_response({
        "supervisor": {
            "pid": pid,
            "alive": alive,
            "pid_file": str(runtime_dir(home) / "supervisor.pid"),
        },
        "backend": {
            "uptime_seconds": uptime,
            "pid": os.getpid(),
            "recovery_state": request.app.get("recovery_state") or "ready",
        },
        "intent": intent,
        "crash_storm": crash_storm,
        "last_recovery": _serialize_recovery_summary(
            request.app.get("last_recovery_summary"),
            agenda_store=request.app.get("agenda_store"),
        ),
        "runtime_dir": str(runtime_dir(home)),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


def _serialize_recovery_summary(
    summary: Any,
    *,
    agenda_store: Any = None,
) -> dict[str, Any] | None:
    """Convert the in-memory RecoverySummary to the JSON shape the UI
    consumes. ``None`` when recovery hasn't run yet (cold backend
    booted without going through ``_run_recovery``). ``operator_attention``
    is reconciled against the live agenda store so post-boot resolutions
    drop out of the snapshot."""
    if summary is None:
        return None
    try:
        payload = summary.to_payload()
        return {
            "boot_id": payload.get("boot_id"),
            "continuation_id": payload.get("continuation_id"),
            "downtime_seconds": payload.get("downtime_seconds"),
            "scans": payload.get("scans") or {},
            "operator_attention": filter_live_attention(
                payload.get("operator_attention") or [],
                agenda_store,
            ),
            "started_at": summary.started_at.isoformat() if hasattr(summary, "started_at") else None,
        }
    except Exception:
        log.exception("runtime: serialize recovery_summary failed")
        return None


async def post_shutdown(request: web.Request) -> web.Response:
    """Operator-initiated clean shutdown. Writes intent then triggers
    aiohttp's on_shutdown chain via ``loop.stop()`` after a short
    delay — same effect as a SIGTERM but without involving the
    supervisor at the signal layer.

    Auth gate matches ``agent_promote``: caller must pass a
    ``session_id`` that resolves to an active operator server session.
    Anonymous + unknown sessions → 401.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "body must be JSON"}, status=400)

    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return web.json_response(
            {"error": "session_id required (operator chat session)"},
            status=401,
        )
    server_session = request.app.get("server_sessions", {}).get(session_id)
    if server_session is None or getattr(
        getattr(server_session, "chat_session", None), "ask_fn", None,
    ) is None:
        return web.json_response(
            {"error": f"operator session {session_id!r} not connected"},
            status=401,
        )

    reason = body.get("reason") or "operator clicked shutdown"
    if not isinstance(reason, str):
        reason = str(reason)

    # Write intent BEFORE triggering shutdown so the supervisor finds
    # it on backend exit. ``on_aiohttp_shutdown`` preserves a same-PID
    # intent already on disk, so this ``ui_button`` source label
    # survives the hook (see ``lifecycle._has_fresh_route_intent``).
    from tesseract.mirror.server.lifecycle import write_shutdown_intent
    write_shutdown_intent(
        intent="operator_quit",
        source="ui_button",
        reason=reason,
    )
    log.info("runtime: POST /api/runtime/shutdown — operator_quit via session=%s", session_id)

    # Trigger aiohttp shutdown out-of-band so the HTTP response can
    # still flush. asyncio.get_event_loop().stop() ends the run loop
    # AFTER the current task completes.
    import asyncio
    loop = asyncio.get_running_loop()
    loop.call_later(0.5, loop.stop)

    return web.json_response({
        "status": "shutting_down",
        "intent": "operator_quit",
        "source": "ui_button",
        "reason": reason,
    })


_LOCALHOST_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _is_localhost_request(request: web.Request) -> bool:
    """True when the request originated from the same machine.

    Mirror binds 127.0.0.1 only (``tesseract/config/mirror.yaml::host``)
    so any inbound connection IS already local. Belt-and-braces: re-check
    ``remote`` so a hypothetical future bind-change can't accidentally
    expose this endpoint without an auth review."""
    remote = (request.remote or "").strip()
    return remote in _LOCALHOST_HOSTS


async def post_restart_for_code_drift(request: web.Request) -> web.Response:
    """Operator-clicked restart in response to a `code_drift_detected`
    toast. Writes ``intent.json {restart_upgrade}`` so the supervisor
    respawns the backend on clean exit, then triggers aiohttp shutdown.

    Auth: accepts either (a) an authenticated operator session, OR
    (b) any localhost caller (Mirror binds 127.0.0.1 only — the threat
    model is "operator at the local machine"; the session gate was
    blocking restart from cold-boot windows where no chat session
    exists yet). Every accepted call is logged with source IP for
    audit. No quiesce — the toast already warned about in-flight work.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "body must be JSON"}, status=400)

    session_id = body.get("session_id")
    auth_path: str
    if isinstance(session_id, str) and session_id:
        server_session = request.app.get("server_sessions", {}).get(session_id)
        if server_session is None or getattr(
            getattr(server_session, "chat_session", None), "ask_fn", None,
        ) is None:
            # Stale or unknown session_id — fall through to the localhost
            # gate rather than 401-ing, so a cold-boot frontend that
            # still holds an old session_id can recover via restart.
            if not _is_localhost_request(request):
                return web.json_response(
                    {"error": f"operator session {session_id!r} not connected"},
                    status=401,
                )
            auth_path = "localhost_stale_session"
        else:
            auth_path = "operator_session"
    else:
        if not _is_localhost_request(request):
            return web.json_response(
                {"error": "operator session required (remote caller)"},
                status=401,
            )
        auth_path = "localhost_no_session"

    head_sha = body.get("head_sha")
    short = (str(head_sha)[:8]) if isinstance(head_sha, str) and head_sha else "dirty"
    cont_id = f"code-drift-{short}"
    reason = body.get("reason") or "operator clicked restart on code drift"
    if not isinstance(reason, str):
        reason = str(reason)

    home = Path(os.environ.get("TESSERACT_HOME") or TESSERACT_HOME).resolve()
    write_atomic(
        intent_path(home),
        IntentFile(
            intent="restart_upgrade",
            timestamp=now_utc(),
            source="ui_button",
            continuation_id=cont_id,
            reason=reason,
            backend_pid=os.getpid(),
            backend_ppid=os.getppid(),
        ),
    )
    log.info(
        "runtime: POST /api/runtime/restart_for_code_drift — "
        "continuation=%s auth=%s session=%s remote=%s",
        cont_id, auth_path, session_id or "<none>", request.remote or "<unknown>",
    )

    import asyncio
    loop = asyncio.get_running_loop()
    loop.call_later(0.5, loop.stop)

    return web.json_response({
        "status": "restarting",
        "intent": "restart_upgrade",
        "continuation_id": cont_id,
        "reason": reason,
    })


def register(app: web.Application) -> None:
    """Register the runtime routes. Called from ``app.py::_routes``."""
    app.router.add_get("/api/runtime/status", get_status)
    app.router.add_post("/api/runtime/shutdown", post_shutdown)
    app.router.add_post("/api/runtime/restart_for_code_drift", post_restart_for_code_drift)


__all__ = ["register", "get_status", "post_shutdown", "post_restart_for_code_drift"]
