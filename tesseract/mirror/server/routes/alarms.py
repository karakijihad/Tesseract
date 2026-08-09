"""REST surface for the Mirror alarms panel.

Mirrors the WS commands in ``server/commands.py::cmd_alarm_*`` but as
plain HTTP for the operator UI panel. Backed by the same
``app["alarm_registry"]`` instance, so changes here are visible to every
surface (the assistant tools, REPL slash commands, WS commands) immediately.
"""

from __future__ import annotations

from datetime import datetime, timezone

from aiohttp import web

from tesseract.scheduler.alarm_parser import (
    ALARM_HANDLER_DOTPATH,
    parse_alarm_spec,
    parse_alarm_when,
)


def _alarm_to_dict(alarm) -> dict:
    return {
        "id": alarm.id,
        "label": alarm.label,
        "run_at": alarm.run_at.isoformat(),
        "message": alarm.message,
        "recurrence": alarm.recurrence.to_dict() if alarm.recurrence else None,
        "created_at": alarm.created_at.isoformat(),
    }


async def create_alarm(request: web.Request) -> web.Response:
    """POST /api/alarms — create a new pending alarm.

    Body: ``{"label": str, "when": str, "message": str?}``. ``when`` accepts
    the same grammar as ``/alarm-set``: ``"30m"``, ``"every 1h"``,
    ``"daily 09:00"``, etc. Recurrence is parsed out of ``when``; a separate
    field would duplicate state and risk drifting from the parser.
    """
    registry = request.app.get("alarm_registry")
    if registry is None:
        return web.json_response({"error": "alarm_registry not ready"}, status=503)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    label = str(body.get("label") or "").strip()
    when = str(body.get("when") or "").strip()
    message = str(body.get("message") or "")
    if not label:
        return web.json_response({"error": "label is required"}, status=400)
    if not when:
        return web.json_response({"error": "when is required"}, status=400)
    now = datetime.now(timezone.utc)
    run_at, recurrence, _trailing = parse_alarm_spec(when, now)
    if run_at is None:
        return web.json_response(
            {"error": f"cannot parse when: {when!r}"},
            status=400,
        )
    try:
        alarm = registry.add(
            label=label,
            run_at=run_at,
            handler_dotpath=ALARM_HANDLER_DOTPATH,
            message=message,
            recurrence=recurrence,
        )
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=409)
    return web.json_response({"alarm": _alarm_to_dict(alarm)}, status=201)


async def list_alarms(request: web.Request) -> web.Response:
    """GET /api/alarms — pending alarms."""
    registry = request.app.get("alarm_registry")
    if registry is None:
        return web.json_response({"alarms": []})
    return web.json_response({
        "alarms": [_alarm_to_dict(a) for a in registry.list_pending()],
    })


async def cancel_alarm(request: web.Request) -> web.Response:
    """DELETE /api/alarms/{handle} — cancel by label or id-prefix."""
    registry = request.app.get("alarm_registry")
    if registry is None:
        return web.json_response({"error": "alarm_registry not ready"}, status=503)
    handle = request.match_info["handle"]
    removed = registry.cancel(handle)
    if removed is None:
        return web.json_response(
            {"error": f"no alarm matching {handle!r}", "suggestions": registry.suggestions(handle)},
            status=404,
        )
    return web.json_response({"cancelled": _alarm_to_dict(removed)})


async def snooze_alarm(request: web.Request) -> web.Response:
    """POST /api/alarms/{handle}/snooze — body ``{"duration": "10m"}`` (default 10m)."""
    registry = request.app.get("alarm_registry")
    if registry is None:
        return web.json_response({"error": "alarm_registry not ready"}, status=503)
    handle = request.match_info["handle"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    duration = str(body.get("duration") or "10m")
    now = datetime.now(timezone.utc)
    run_at = parse_alarm_when(duration, now)
    if run_at is None or run_at <= now:
        return web.json_response(
            {"error": f"cannot parse snooze duration: {duration!r}"},
            status=400,
        )
    alarm = registry.snooze(handle, run_at)
    if alarm is None:
        return web.json_response(
            {"error": f"no alarm matching {handle!r}", "suggestions": registry.suggestions(handle)},
            status=404,
        )
    return web.json_response({"snoozed": _alarm_to_dict(alarm)})
