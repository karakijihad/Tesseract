"""Brief tab REST routes — MO-9-9 (operator-facing daily-brief surface).

Read-heavy: list/detail are pure file reads from
``<TESSERACT_HOME>/memory-store/daily/briefs/``. The single write action
— ``POST /refresh`` — dispatches ``execute_tool("brief_render", ...)``;
``BriefRenderTool`` is ASK-gated at the policy layer so the operator is
prompted in their chat session before the renderer fires.

MO-9-14 adds ``POST /api/brief/feedback`` which mutates the operator's
interest-affinity profile. Concurrent POSTs serialise on
``_FEEDBACK_LOCK`` so a load→modify→save race cannot silently drop a
signal — the phase-gate reviewer flagged the lost-update window as an
exit-criterion gap and this lock is the fold.

Authoritative contracts:
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from tesseract.brain.tools import execute_tool
from tesseract.kernel.tools.base import ToolContext
from tesseract.paths import TESSERACT_HOME

log = logging.getLogger(__name__)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Module-level asyncio lock serialising `brief_feedback` writes. The
# route loads the profile, applies a signal, then writes — without
# this lock, two POSTs landing inside the same event-loop tick could
# both read the pre-update profile and one save would silently win.
# Single operator + UI-side per-card guard makes this paper-thin in
# practice, but the phase doc §8 exit criterion explicitly requires
# atomicity. asyncio.Lock is process-local; multi-process deployments
# would need the cross-process EventStore-style file lock.
_FEEDBACK_LOCK = asyncio.Lock()


def _briefs_dir() -> Path:
    # Resolve TESSERACT_HOME at call time so monkeypatched env vars in
    # tests reach the writer/reader. Same pattern as
    # ``workspace_changes.workspace_events_dir``, which is the only writer.
    home = Path(__import__("os").environ.get("TESSERACT_HOME") or TESSERACT_HOME).resolve()
    return home / "memory-store" / "daily" / "briefs"


def _is_iso_date(raw: str) -> bool:
    if not _ISO_DATE.match(raw):
        return False
    try:
        datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _strip_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a brief file into (frontmatter_dict, body).

    The renderer writes ``---\\n<yaml>---\\n\\n<body>``; we partition on
    the ``---\\n\\n`` boundary (same as the MO-9-8 voice-safety test) so
    the body always starts at the title heading. Frontmatter is parsed
    line-by-line into a flat ``key: value`` dict — nested lists (``sources:``)
    are not surfaced to the frontend; the operator-facing UI only needs
    the scalars.
    """
    if not text.startswith("---\n"):
        return {}, text
    head, sep, body = text[4:].partition("---\n")
    if not sep:
        return {}, text
    fm: dict[str, str] = {}
    for line in head.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("-") or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        fm[key.strip()] = value.strip()
    return fm, body.lstrip("\n")


async def get_brief_dates(request: web.Request) -> web.Response:
    """GET /api/brief/dates — list of ISO dates with a brief on disk.

    Newest-first. Returns ``{dates: [...]}`` even when the directory
    does not exist yet (first-run / TAVILY-disabled boxes).
    """
    root = _briefs_dir()
    if not root.exists():
        return web.json_response({"dates": []})
    dates: list[str] = []
    for child in root.iterdir():
        if not child.is_file() or child.suffix != ".md":
            continue
        stem = child.stem
        if _is_iso_date(stem):
            dates.append(stem)
    dates.sort(reverse=True)
    return web.json_response({"dates": dates})


async def get_brief(request: web.Request) -> web.Response:
    """GET /api/brief/{date} — return the brief markdown for ``date``.

    Body shape::

        {
          "date": "2026-05-14",
          "path": "memory-store/daily/briefs/2026-05-14.md",
          "frontmatter": {...},
          "body": "# Daily Brief — 2026-05-14\\n\\n...",
        }

    404 when the file does not exist. 400 when the date is not ISO.
    """
    raw = request.match_info["date"]
    if not _is_iso_date(raw):
        return web.json_response(
            {"error": f"date must be ISO YYYY-MM-DD, got {raw!r}"}, status=400,
        )
    path = _briefs_dir() / f"{raw}.md"
    if not path.exists():
        return web.json_response({"error": f"no brief for {raw}"}, status=404)
    text = path.read_text(encoding="utf-8")
    frontmatter, body = _strip_frontmatter(text)
    return web.json_response(
        {
            "date": raw,
            "path": str(path),
            "frontmatter": frontmatter,
            "body": body,
        }
    )


async def refresh_brief(request: web.Request) -> web.Response:
    """POST /api/brief/refresh — re-run today's brief via ``brief_render``.

    Body: ``{"session_id": str, "date"?: str}``. ``brief_render`` is
    ASK-gated by ``permissions.yaml::tools.brief_render``; the operator's
    chat session ``ask_fn`` handles the prompt. ``overwrite=True`` matches
    the ``/brief`` slash semantics from MO-9-8.

    The ``daily_brief_ready`` WS broadcast is emitted by ``brief_render``'s
    side-effect path (see ``tesseract/orchestrator/brief/renderer.py``'s
    callback hook — wired here through the tool result's metadata.path on
    approval). For now, we fan out the envelope on successful approval so
    the frontend store + toast wake up without a manual reload.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)

    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return web.json_response(
            {"error": "session_id required (operator chat session)"}, status=400,
        )
    date_raw = body.get("date")
    target_date = ""
    if isinstance(date_raw, str) and date_raw:
        if not _is_iso_date(date_raw):
            return web.json_response(
                {"error": f"date must be ISO YYYY-MM-DD, got {date_raw!r}"},
                status=400,
            )
        target_date = date_raw

    registry = request.app.get("tool_registry")
    if registry is None or registry.get("brief_render") is None:
        return web.json_response(
            {"error": "tool_registry not ready; brief_render unavailable"},
            status=503,
        )

    server_session = request.app.get("server_sessions", {}).get(session_id)
    ask_fn = getattr(server_session.chat_session, "ask_fn", None) if server_session else None
    if ask_fn is None:
        return web.json_response(
            {"error": f"operator session {session_id!r} not connected"},
            status=503,
        )

    context = ToolContext(
        session_id=session_id,
        ask_fn=ask_fn,
    )
    try:
        result = await execute_tool(
            registry,
            "brief_render",
            {"date": target_date, "overwrite": True},
            context,
            ask_fn=ask_fn,
        )
    except Exception as exc:
        log.exception("brief refresh: execute_tool raised")
        return web.json_response(
            {"status": "denied", "output": f"tool dispatch failed: {exc}"},
            status=500,
        )

    payload: dict[str, Any] = {
        "status": "denied" if result.is_error else "approved",
        "output": result.output,
        "metadata": dict(result.metadata or {}),
    }

    # Fan out a daily_brief_ready envelope on approval so the toast
    # wakes up without a manual reload. We do this here (after the
    # operator approved + the renderer wrote) rather than inside the
    # renderer because the renderer has no Mirror app handle. The
    # workspace event the renderer also wrote is fanned out separately
    # via `_broadcast_workspace_event_for_brief` so the inbox refresh
    # picks up the new newsletter card.
    if not result.is_error:
        path = (result.metadata or {}).get("path")
        date_str = target_date or datetime.now(timezone.utc).date().isoformat()
        if isinstance(path, str):
            await broadcast_daily_brief_ready(
                request.app,
                date=date_str,
                path=path,
                summary=(result.output or "").strip().splitlines()[0] if result.output else "",
            )
        workspace_event_id = (result.metadata or {}).get("workspace_event_id")
        if isinstance(workspace_event_id, str) and workspace_event_id:
            await _broadcast_workspace_event_for_brief(
                request.app, event_id=workspace_event_id,
            )

    return web.json_response(payload)


async def _broadcast_workspace_event_for_brief(
    app: web.Application,
    *,
    event_id: str,
) -> None:
    """Fan the newsletter card out as `workspace_event_appended` so
    every open Mirror inbox refreshes when /api/brief/refresh writes
    a new brief. Fail-soft: a missed broadcast doesn't roll back the
    disk write."""
    store = app.get("workspace_event_store") if hasattr(app, "get") else None
    if store is None:
        return
    try:
        from tesseract.workspace_events.broadcast import broadcast_workspace_event
    except Exception:
        log.exception("brief refresh: workspace_events import failed")
        return
    try:
        event = store.get_event(event_id)
    except Exception:
        log.exception("brief refresh: get_event failed")
        return
    if event is None:
        return
    try:
        await broadcast_workspace_event(app, event)
    except Exception:
        log.exception("brief refresh: workspace broadcast failed")


async def broadcast_daily_brief_ready(
    app: web.Application,
    *,
    date: str,
    path: str,
    summary: str,
) -> None:
    """Fan a ``daily_brief_ready`` envelope out to every Mirror WS.

    Mirrors :func:`tesseract.workspace_events.broadcast.broadcast_workspace_event`
    — same fail-soft pattern. Imported lazily by the scheduler-side
    ``DailyBriefJob`` so the cron path can fire the same envelope when
    ``BriefRenderer.render`` completes outside the REST surface.
    """
    sessions = app.get("server_sessions") or {}
    if not sessions:
        return
    try:
        from tesseract.mirror.server.envelope import make_daily_brief_ready
        from tesseract.mirror.server.session import send_envelope
    except Exception:
        log.exception("brief broadcast: envelope/session import failed")
        return
    for sess in list(sessions.values()):
        env = make_daily_brief_ready(
            getattr(sess, "session_id", ""),
            date=date,
            path=path,
            summary=summary,
        )
        try:
            await send_envelope(sess, env)
        except Exception:
            log.exception(
                "brief broadcast: send_envelope failed for %s",
                getattr(sess, "session_id", "?"),
            )

    # MO-10-3 — fire the daily-brief Telegram push subscriber if wired.
    # Fail-soft: a busted push must not be reported as a failed broadcast.
    push = app.get("brief_push_subscriber")
    if push is not None:
        try:
            await push.handle()
        except Exception:
            log.exception("brief broadcast: brief_push subscriber failed")


async def brief_feedback(request: web.Request) -> web.Response:
    """POST /api/brief/feedback — apply a per-card signal to the interests profile.

    Body: ``{"date": "YYYY-MM-DD", "pillar": str, "url": str, "signal":
    one of "interested"|"not_for_me"|"dig_deeper"|"commented",
    "topic"?: str}``.

    ``topic`` is optional — the signal is recorded against ``topic``
    when given, otherwise against the card's ``url`` (so the URL becomes
    a topic-key and the operator's affinity for that specific source
    accumulates). The implementation is intentionally simple — Pydantic
    is overkill for a four-field POST and the route keeps validation
    inline so a malformed body returns a precise 400.

    Returns the updated affinity dict for the pillar so the UI can
    micro-animate the card's "learning" state.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)

    date_raw = body.get("date")
    if not isinstance(date_raw, str) or not _is_iso_date(date_raw):
        return web.json_response(
            {"error": "date must be ISO YYYY-MM-DD"}, status=400,
        )
    pillar = body.get("pillar")
    if not isinstance(pillar, str) or not pillar.strip():
        return web.json_response({"error": "pillar required"}, status=400)
    pillar = pillar.strip()
    signal_raw = body.get("signal")
    if not isinstance(signal_raw, str):
        return web.json_response({"error": "signal required"}, status=400)
    try:
        from tesseract.orchestrator.brief.interests import (
            Signal,
            load_profile,
            record_signal,
            save_profile,
        )
    except Exception:
        log.exception("brief_feedback: interests module import failed")
        return web.json_response({"error": "interests substrate unavailable"}, status=500)
    try:
        signal = Signal(signal_raw.strip().upper())
    except ValueError:
        valid = ", ".join(s.value.lower() for s in Signal)
        return web.json_response(
            {"error": f"signal must be one of: {valid}"}, status=400,
        )

    url = str(body.get("url") or "").strip()
    topic = str(body.get("topic") or "").strip() or url
    if not topic:
        return web.json_response(
            {"error": "either topic or url required to key the signal"},
            status=400,
        )

    async with _FEEDBACK_LOCK:
        profile = load_profile()
        updated = record_signal(profile, pillar, topic, signal)
        try:
            save_profile(updated)
        except Exception:
            log.exception("brief_feedback: save_profile failed")
            return web.json_response({"error": "failed to persist profile"}, status=500)
        affinity = dict(updated.pillars.get(pillar) or {})

    return web.json_response({
        "date": date_raw,
        "pillar": pillar,
        "topic": topic,
        "signal": signal.value,
        "affinity": affinity,
    })


__all__ = [
    "brief_feedback",
    "broadcast_daily_brief_ready",
    "get_brief",
    "get_brief_dates",
    "refresh_brief",
]
