"""TC-5 — Mirror → controller reload bridge.

When the Mirror config watcher detects a change in ``providers.yaml`` /
``roles.yaml`` / ``permissions.yaml``, it rebuilds its own in-process
adapters and then fires this helper to ask the running controller
daemon (if any) to drain its in-flight turns and reload.

The bridge is **best-effort and short-timeout**. Mirror must not block
on the controller — a missing controller, a stale port file, an auth
failure, or a slow drain must all collapse to ``{ok: False, ...}``
without throwing into the watcher callback.

TC-6's ``ControllerClient`` will reuse the same connection / auth
sequence at the call-site level; for now we keep the bridge as a
narrow function so it is trivial to remove if TC-6 lands a richer
client first.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from tesseract.kernel.sandbox._ipc_frames import decode_frame, encode_frame

from . import auth as controller_auth
from .paths import port_file_path

log = logging.getLogger(__name__)


ReloadTarget = Literal["config", "roles", "tools", "all"]


async def notify_controller_reload(
    target: ReloadTarget,
    *,
    connect_timeout: float = 1.0,
    response_timeout: float = 60.0,
) -> dict[str, Any]:
    """Open a one-shot IPC connection, auth, send ``reload``, read the
    ``reload_complete`` push, close. Returns the parsed push payload on
    success or ``{ok: False, code: <reason>}`` on every other path.

    ``connect_timeout`` is intentionally short (1s default) so a dead
    controller does not stall the Mirror watcher. ``response_timeout``
    covers the controller-side drain (default 60s ≥
    ``drain_timeout_seconds`` + a slack budget).
    """

    path = port_file_path()
    if not path.exists():
        return {"ok": False, "code": "no_controller", "detail": "port file missing"}
    try:
        port = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as exc:
        return {"ok": False, "code": "port_unreadable", "detail": str(exc)}
    if port <= 0 or port > 65535:
        return {"ok": False, "code": "port_out_of_range", "detail": str(port)}

    token = controller_auth.read_token()
    if not token:
        return {"ok": False, "code": "no_token", "detail": "token file missing"}

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port),
            timeout=connect_timeout,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        return {"ok": False, "code": "connect_failed", "detail": str(exc)}

    try:
        writer.write(encode_frame({"auth": token}))
        writer.write(encode_frame({"msg": "reload", "target": target}))
        await writer.drain()

        # Wall-clock deadline — every intermediate push (session_status,
        # transcript_event) chews into the same budget rather than
        # restarting the timer. Without this, N intermediate pushes
        # could stall the Mirror watcher for N × ``response_timeout``.
        loop = asyncio.get_event_loop()
        deadline_at = loop.time() + response_timeout
        while True:
            remaining = deadline_at - loop.time()
            if remaining <= 0:
                return {"ok": False, "code": "response_timeout", "detail": ""}
            try:
                payload = await asyncio.wait_for(
                    decode_frame(reader), timeout=remaining
                )
            except asyncio.TimeoutError:
                return {"ok": False, "code": "response_timeout", "detail": ""}
            except asyncio.IncompleteReadError:
                return {"ok": False, "code": "connection_closed", "detail": ""}
            except ValueError as exc:  # oversize / malformed frame
                log.error("reload bridge: malformed frame, closing: %s", exc)
                return {"ok": False, "code": "malformed_frame", "detail": str(exc)}
            event = payload.get("event")
            if event == "error":
                return {
                    "ok": False,
                    "code": payload.get("code", "error"),
                    "detail": payload.get("detail", ""),
                }
            if event == "reload_complete":
                return {"ok": True, **payload}
            # transcript_event / session_status / ack — keep reading.
    except (OSError, ConnectionError) as exc:  # noqa: BLE001
        return {"ok": False, "code": "io_error", "detail": str(exc)}
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


__all__ = ["ReloadTarget", "notify_controller_reload"]
