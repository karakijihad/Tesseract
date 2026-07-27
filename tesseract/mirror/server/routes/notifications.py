"""Outbound notification settings — AU-10.

Three endpoints back the autonomy dashboard's notification panel:

- ``GET  /api/notifications/config`` — anonymous-readable. Returns the
  full category catalog (with the operator-visible exempt flag) and the
  current mute state per channel. Reads YAML mutes + runtime mutes and
  unions them.
- ``POST /api/notifications/mute``   — operator-session-gated. Body
  ``{session_id, channel, category, muted}``. Writes the runtime mute
  override at ``<HOME>/runtime/outbound-mutes.json``; the YAML mute
  list is hand-edited only.
- ``GET  /api/notifications/rates``  — anonymous-readable. Returns the
  current sliding-window counts per (channel, category) plus the
  per-category cap so the dashboard can show "5/6 used".

The audit shape mirrors ``agenda.py`` — ``_authed_body`` on mutating
endpoints; anonymous on reads.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from tesseract.orchestrator.autonomy.outbound import (
    CATEGORIES,
    DEFAULT_RATE_PER_HOUR,
    EXEMPT_CATEGORIES,
    RateLedger,
    read_runtime_mutes,
    write_runtime_mutes,
)

log = logging.getLogger(__name__)


def _channels_config(request: web.Request) -> Any | None:
    return request.app.get("channels_config")


def _telegram_block(request: web.Request) -> Any | None:
    cfg = _channels_config(request)
    if cfg is None:
        return None
    block_fn = getattr(cfg, "channel_block", None)
    if callable(block_fn):
        return block_fn("telegram")
    return getattr(cfg, "telegram", None)


def _yaml_mutes(request: web.Request, channel_name: str) -> list[str]:
    if channel_name != "telegram":
        return []
    block = _telegram_block(request)
    if block is None:
        return []
    raw = getattr(block, "muted_categories", None)
    if isinstance(raw, (list, tuple)):
        return [str(c) for c in raw]
    return []


def _cap_for(request: web.Request, channel_name: str, category: str) -> int:
    if channel_name != "telegram":
        return DEFAULT_RATE_PER_HOUR
    block = _telegram_block(request)
    if block is None:
        return DEFAULT_RATE_PER_HOUR
    rate_block = getattr(block, "outbound_rate", None)
    if rate_block is None:
        return DEFAULT_RATE_PER_HOUR
    per_category = getattr(rate_block, "per_category", None) or {}
    if isinstance(per_category, dict) and category in per_category:
        try:
            return int(per_category[category])
        except (TypeError, ValueError):
            pass
    default_cap = getattr(rate_block, "default_per_hour", None)
    try:
        return int(default_cap) if default_cap is not None else DEFAULT_RATE_PER_HOUR
    except (TypeError, ValueError):
        return DEFAULT_RATE_PER_HOUR


def _require_operator_session(
    request: web.Request, body: dict[str, Any],
) -> web.Response | None:
    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return web.json_response(
            {"error": "session_id required (operator chat session)"}, status=401
        )
    server_session = request.app.get("server_sessions", {}).get(session_id)
    chat_session = getattr(server_session, "chat_session", None) if server_session else None
    if chat_session is None or getattr(chat_session, "ask_fn", None) is None:
        return web.json_response(
            {"error": f"operator session {session_id!r} not connected"}, status=401
        )
    return None


async def _authed_body(
    request: web.Request,
) -> tuple[dict[str, Any] | None, web.Response | None]:
    try:
        body = await request.json()
    except Exception:
        return None, web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return None, web.json_response(
            {"error": "body must be a JSON object"}, status=400,
        )
    err = _require_operator_session(request, body)
    if err is not None:
        return None, err
    return body, None


async def get_config(request: web.Request) -> web.Response:
    runtime_mutes = read_runtime_mutes()
    channels: list[dict[str, Any]] = []
    for channel_name in ("telegram",):
        block = _telegram_block(request)
        muted_yaml = _yaml_mutes(request, channel_name)
        muted_runtime = runtime_mutes.get(channel_name, [])
        muted_effective = sorted(set(muted_yaml) | set(muted_runtime))
        channels.append({
            "name": channel_name,
            "enabled": bool(getattr(block, "enabled", channel_name == "telegram")),
            "muted_yaml": muted_yaml,
            "muted_runtime": muted_runtime,
            "muted_effective": muted_effective,
        })
    catalog = [
        {
            "category": cat,
            "exempt": cat in EXEMPT_CATEGORIES,
        }
        for cat in CATEGORIES
    ]
    return web.json_response({"categories": catalog, "channels": channels})


async def post_mute(request: web.Request) -> web.Response:
    body, err = await _authed_body(request)
    if err is not None:
        return err
    assert body is not None
    channel = body.get("channel")
    category = body.get("category")
    muted = body.get("muted")
    if not isinstance(channel, str) or not channel:
        return web.json_response({"error": "channel required"}, status=400)
    if not isinstance(category, str) or category not in CATEGORIES:
        return web.json_response({"error": f"unknown category {category!r}"}, status=400)
    if not isinstance(muted, bool):
        return web.json_response({"error": "muted must be boolean"}, status=400)
    if muted and category in EXEMPT_CATEGORIES:
        return web.json_response(
            {
                "error": (
                    f"{category!r} is exempt per GOVERNANCE §9 — "
                    "operator MUST see it; cannot be muted"
                )
            },
            status=400,
        )

    runtime_mutes = read_runtime_mutes()
    current = set(runtime_mutes.get(channel, []))
    if muted:
        current.add(category)
    else:
        current.discard(category)
    if current:
        runtime_mutes[channel] = sorted(current)
    else:
        runtime_mutes.pop(channel, None)
    write_runtime_mutes(runtime_mutes)

    return web.json_response({
        "channel": channel,
        "category": category,
        "muted": muted,
        "muted_runtime": runtime_mutes.get(channel, []),
    })


async def get_rates(request: web.Request) -> web.Response:
    notifier = request.app.get("outbound_notifier")
    ledger = notifier.ledger if notifier is not None else RateLedger()
    rows: list[dict[str, Any]] = []
    for channel_name in ("telegram",):
        for cat in CATEGORIES:
            cap = _cap_for(request, channel_name, cat)
            used = ledger.count(channel_name, cat)
            rows.append({
                "channel": channel_name,
                "category": cat,
                "cap_per_hour": cap,
                "used_last_hour": used,
                "exempt": cat in EXEMPT_CATEGORIES,
            })
    return web.json_response({"rows": rows})


def register(app: web.Application) -> None:
    app.router.add_get("/api/notifications/config", get_config)
    app.router.add_post("/api/notifications/mute", post_mute)
    app.router.add_get("/api/notifications/rates", get_rates)


__all__ = ["get_config", "get_rates", "post_mute", "register"]
