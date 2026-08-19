"""The brief reaches the operator at the hour they chose.

`brief_render` writes the brief as the last stage of the night. This sends it,
and the two are apart on purpose: **generation is the system's business and
delivery is the operator's.** The hour lives in `mirror.yaml::brief`, not in
`schedule.yaml`, so moving it moves this and nothing else — where the 08:00
row it replaces was a wall clock chosen on one machine that shipped to every
install and did both jobs at once.

A service rather than a row for the same reason. The loop wakes on a short
interval and asks one question — is it past today's delivery time, and has
today's brief already gone out — which means a changed hour takes effect
immediately, and a machine asleep at the hour delivers when it wakes instead
of skipping the day. A long sleep to the exact minute would fail both.

Delivered-once is a marker file, not a memory: the backend restarts, and a
brief sent twice is worse than one sent a minute late.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, time as clock_time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# How often the loop asks whether the hour has arrived. Not the delivery
# precision an operator cares about — they set an hour, not a minute — and
# short enough that a config change or a wake-from-sleep is picked up while
# they are still at the machine.
_WAKE_INTERVAL_S = 60.0

_DEFAULT_HOUR = 8
_DEFAULT_MINUTE = 0


def _marker_path() -> Path:
    from tesseract.paths import runtime_dir

    return runtime_dir() / "brief-delivered.json"


def delivery_time() -> clock_time:
    """The operator's hour, from `mirror.yaml::brief`. Falls back to 08:00 —
    the hour the row it replaces used — when the block is absent, because a
    missing preference must not mean the brief stops arriving."""
    from tesseract.mirror.server.config import _load_yaml, MIRROR_YAML

    block = (_load_yaml(MIRROR_YAML) or {}).get("brief") or {}
    hour = block.get("delivery_hour", _DEFAULT_HOUR)
    minute = block.get("delivery_minute", _DEFAULT_MINUTE)
    try:
        return clock_time(hour=int(hour), minute=int(minute))
    except (TypeError, ValueError):
        log.warning(
            "brief_delivery: mirror.yaml::brief names %r:%r, which is not a "
            "time of day — falling back to %02d:%02d",
            hour, minute, _DEFAULT_HOUR, _DEFAULT_MINUTE,
        )
        return clock_time(hour=_DEFAULT_HOUR, minute=_DEFAULT_MINUTE)


def _last_delivered() -> str:
    try:
        return str(json.loads(_marker_path().read_text(encoding="utf-8")).get("date") or "")
    except (OSError, ValueError):
        return ""


def _mark_delivered(brief_date: str) -> None:
    path = _marker_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"date": brief_date}), encoding="utf-8")
    except OSError:
        log.warning("brief_delivery: could not write %s", path, exc_info=True)


def is_due(now: datetime, *, at: clock_time, last_delivered: str) -> bool:
    """Past today's hour, and today's brief has not gone out.

    Local time, like every cadence the operator sets — `schedule.yaml`'s cron
    is read in system local time for the same reason, and an hour meaning
    something other than what the clock on the wall says is the one thing a
    preference must not do.
    """
    return now.time() >= at and last_delivered != now.date().isoformat()


def brief_path(brief_date: date) -> Path:
    """Where `brief_render` wrote it. Same resolution the renderer uses —
    `TESSERACT_HOME`, read at call time, not the source tree."""
    import os

    from tesseract.paths import TESSERACT_HOME

    home = Path(os.environ.get("TESSERACT_HOME") or TESSERACT_HOME).resolve()
    return home / "memory-store" / "daily" / "briefs" / f"{brief_date.isoformat()}.md"


def _latest_brief_event(app: Any, *, brief_date: date) -> Any | None:
    """The `daily_brief` workspace event for this date, or None.

    The event is what carries the summary and the id both delivery paths
    need — the payload the renderer stores holds the sections and the date
    and neither of those.
    """
    store = app.get("workspace_event_store") if hasattr(app, "get") else None
    if store is None:
        return None
    try:
        events = store.list_events(kinds=("daily_brief",), limit=5)
    except Exception:  # noqa: BLE001
        log.exception("brief_delivery: list_events failed")
        return None
    wanted = brief_date.isoformat()
    for event in events:
        payload = getattr(event, "payload", None) or {}
        if isinstance(payload, dict) and str(payload.get("date") or "") == wanted:
            return event
    return None


async def deliver_now(app: Any, *, brief_date: date) -> dict[str, Any]:
    """Send one brief. Safe to call directly — the service is the only caller
    that also decides WHEN.

    Absence of the event is `no_brief`, NOT a failure: the render stage may
    not have run yet, and the caller must be able to tell "nothing to send"
    from "sending went wrong" or it will mark an undelivered day as done.
    """
    if not brief_path(brief_date).exists():
        return {"delivered": False, "reason": "no_brief"}
    event = _latest_brief_event(app, brief_date=brief_date)
    if event is None:
        return {"delivered": False, "reason": "no_event"}
    await _broadcast_brief_ready(
        app,
        date=brief_date.isoformat(),
        path=str(brief_path(brief_date)),
        summary=str(getattr(event, "summary", "") or ""),
    )
    await _broadcast_workspace_event_for_brief(
        app,
        event_store=app.get("workspace_event_store") if hasattr(app, "get") else None,
        event_id=getattr(event, "id", None),
    )
    return {"delivered": True, "date": brief_date.isoformat()}


async def delivery_loop(app: Any) -> None:
    """Wake, ask whether the hour has arrived, deliver at most once a day."""
    while True:
        try:
            at = delivery_time()
            now = datetime.now()
            if is_due(now, at=at, last_delivered=_last_delivered()):
                result = await deliver_now(app, brief_date=now.date())
                if result.get("delivered"):
                    # Only on a send. A day with no brief to send is not a day
                    # delivered — the render may simply not have run yet, and
                    # marking it would skip the real one when it appears.
                    _mark_delivered(now.date().isoformat())
                    log.info("brief_delivery: sent %s", result.get("date"))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a delivery must not end the loop
            log.exception("brief_delivery: pass failed")
        await asyncio.sleep(_WAKE_INTERVAL_S)


async def _broadcast_workspace_event_for_brief(
    app: Any,
    *,
    event_store: Any,
    event_id: str | None,
) -> None:
    """Fan the daily_brief workspace event out to open Mirror sessions.

    The renderer appended the event to disk; the workspace store's
    fan-out happens at the call site so the inbox refreshes without a
    manual reload. Fail-soft — a missed broadcast doesn't roll back the
    disk write.
    """
    if app is None or event_store is None or not event_id:
        return
    try:
        from tesseract.workspace_events.broadcast import broadcast_workspace_event
    except Exception:  # noqa: BLE001
        log.exception("brief_delivery: workspace_events import failed")
        return
    try:
        event = event_store.get_event(event_id)
    except Exception:  # noqa: BLE001
        log.exception("brief_delivery: get_event failed")
        return
    if event is None:
        return
    try:
        await broadcast_workspace_event(app, event)
    except Exception:  # noqa: BLE001
        log.exception("brief_delivery: send failed")


async def _broadcast_brief_ready(
    app: Any,
    *,
    date: str,
    path: str,
    summary: str,
) -> None:
    """Fan a ``daily_brief_ready`` envelope from the delivery service.

    Imported lazily so this module stays import-safe in REPL / standalone
    contexts where the Mirror is not loaded. Same fail-soft contract as
    ``broadcast_workspace_event``: never raise — a render that succeeded
    on disk must not be reported as failed because a WS pipe went away.
    """
    if app is None or not hasattr(app, "get"):
        return
    sessions = app.get("server_sessions") or {}
    if not sessions:
        await _push_brief_to_telegram(app, date=date)
        return
    try:
        from tesseract.mirror.server.routes.brief import broadcast_daily_brief_ready
    except Exception:
        log.exception("brief_delivery: brief route import failed")
        await _push_brief_to_telegram(app, date=date)
        return
    try:
        await broadcast_daily_brief_ready(app, date=date, path=path, summary=summary)
    except Exception:
        log.exception("brief_delivery: send failed")
        await _push_brief_to_telegram(app, date=date)
        return
    if app.get("brief_push_subscriber") is None:
        await _push_brief_to_telegram(app, date=date)


async def _push_brief_to_telegram(app: Any, *, date: str | None = None) -> None:
    """Fire the Telegram brief-push subscriber without requiring Mirror WS.

    ``broadcast_daily_brief_ready`` owns the normal Mirror-session path and
    already invokes this subscriber after it sends WS envelopes. With no
    open sessions it returns early, so the scheduler calls the subscriber
    directly here. Fail-soft: Telegram delivery must not invalidate the
    canonical brief written to disk.
    """
    if not hasattr(app, "get"):
        return
    push = app.get("brief_push_subscriber")
    if push is None:
        push = _build_brief_push_subscriber(app)
        if push is None:
            await _push_brief_via_telegram_api(app, date=date)
            return
    handle = getattr(push, "handle", None)
    if handle is None:
        await _push_brief_via_telegram_api(app, date=date)
        return
    try:
        result = await handle()
        log.info("brief_delivery: brief_push result=%s", result)
        if _should_try_telegram_api_fallback(result):
            await _push_brief_via_telegram_api(app, date=date)
    except Exception:
        log.exception("brief_delivery: brief_push subscriber failed")
        await _push_brief_via_telegram_api(app, date=date)


def _build_brief_push_subscriber(app: Any) -> Any:
    """Construct a ``TelegramBriefPushSubscriber`` on demand.

    The Mirror app normally wires ``brief_push_subscriber`` at bridge
    startup (``_wire_brief_push_subscriber``). In a scheduler-run app
    context that wiring may be absent, leaving ``_push_brief_to_telegram``
    a no-op. Rebuild it here from the same dependencies the Mirror uses,
    reading config/allowlist/tier at call time. Fail-soft: any missing
    dependency returns None so the disk write stays canonical.
    """
    try:
        from tesseract.integrations.telegram.brief_push import (
            TelegramBriefPushSubscriber,
        )
        from tesseract.integrations.telegram.state import load_allowlist
    except Exception:
        log.exception("brief_delivery: brief_push import failed")
        return None

    telegram_bridge = app.get("telegram_bridge")
    event_store = app.get("workspace_event_store")
    if telegram_bridge is None or getattr(telegram_bridge, "_state", None) is None:
        return None
    if event_store is None:
        return None

    return TelegramBriefPushSubscriber(
        bridge=telegram_bridge,
        event_store=event_store,
        config_loader=lambda: app.get("channels_config"),
        allowlist_loader=lambda: load_allowlist(
            telegram_bridge._state.allowlist_path
        ),
        user_tier_loader=lambda: dict(telegram_bridge._state.poll_state.user_tier),
    )


def _should_try_telegram_api_fallback(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    reason = result.get("reason")
    if reason in {"disabled", "no_payload", "empty_text"}:
        return False
    sent = int(result.get("sent") or 0)
    skipped = int(result.get("skipped") or 0)
    errors = int(result.get("errors") or 0)
    if sent > 0 or skipped > 0:
        return False
    return reason == "no_bridge" or errors > 0


async def _push_brief_via_telegram_api(
    app: Any,
    *,
    date: str | None = None,
) -> dict[str, Any]:
    """Last-resort daily-brief push when the live bridge is unavailable.

    This preserves the operator-facing delivery path for scheduler runs
    where ``TELEGRAM_BOT_TOKEN`` and the allowlist exist but the Mirror
    bridge/subscriber was not wired. It intentionally does not raise:
    the markdown brief and workspace event remain the canonical result.
    """
    if not hasattr(app, "get"):
        return {"sent": 0, "skipped": 0, "errors": 0, "reason": "no_app"}
    if not _telegram_brief_push_enabled(app):
        return {"sent": 0, "skipped": 0, "errors": 0, "reason": "disabled"}

    try:
        from tesseract.integrations.telegram.brief_push import (
            format_exec_summary,
            send_to_operators,
        )
        from tesseract.integrations.telegram.api import TelegramAPI
        from tesseract.integrations.telegram.state import load_allowlist, load_state
    except Exception:
        log.exception("brief_delivery: TelegramAPI fallback import failed")
        return {"sent": 0, "skipped": 0, "errors": 1, "reason": "import_failed"}

    payload = _latest_brief_payload(app, date=date)
    if payload is None:
        return {"sent": 0, "skipped": 0, "errors": 0, "reason": "no_payload"}
    text = format_exec_summary(payload)
    if not text:
        return {"sent": 0, "skipped": 0, "errors": 0, "reason": "empty_text"}

    import os

    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        return {"sent": 0, "skipped": 0, "errors": 0, "reason": "no_token"}

    state_dir = _resolve_telegram_state_dir()
    allowlist = load_allowlist(
        state_dir / "allowlist.json",
        env_seed=os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS"),
    )
    tiers = load_state(state_dir / "state.json").user_tier
    try:
        api = TelegramAPI(token)
    except Exception:
        log.exception("brief_delivery: TelegramAPI fallback init failed")
        return {"sent": 0, "skipped": 0, "errors": 1, "reason": "init_failed"}
    try:
        result = await send_to_operators(
            text,
            bridge=_TelegramAPISender(api),
            allowlist=allowlist,
            user_tier=dict(tiers),
        )
        log.info("brief_delivery: TelegramAPI fallback result=%s", result)
        return result
    except Exception:
        log.exception("brief_delivery: TelegramAPI fallback failed")
        return {"sent": 0, "skipped": 0, "errors": 1, "reason": "send_failed"}
    finally:
        try:
            await api.aclose()
        except Exception:
            log.exception("brief_delivery: TelegramAPI fallback close failed")


def _telegram_brief_push_enabled(app: Any) -> bool:
    cfg = app.get("channels_config") if hasattr(app, "get") else None
    if cfg is None:
        try:
            from tesseract.integrations._channels_config import load_channels_config

            cfg = load_channels_config()
        except Exception:
            log.exception("brief_delivery: channels config load failed")
            return False
    telegram_block = getattr(cfg, "telegram", None)
    if telegram_block is None:
        return False
    return bool(getattr(telegram_block, "brief_push", False))


def _latest_brief_payload(
    app: Any,
    *,
    date: str | None = None,
) -> dict[str, Any] | None:
    event_store = app.get("workspace_event_store") if hasattr(app, "get") else None
    if event_store is None:
        return None
    try:
        events = event_store.list_events(kinds=("daily_brief",), limit=5)
    except Exception:
        log.exception("brief_delivery: list_events failed")
        return None
    for ev in events:
        payload = getattr(ev, "payload", None) or {}
        if not isinstance(payload, dict) or not payload.get("sections"):
            continue
        if date is not None and str(payload.get("date") or "") != date:
            continue
        return payload
    return None


def _resolve_telegram_state_dir() -> Path:
    import os

    return (
        Path(os.environ.get("TESSERACT_HOME") or TESSERACT_HOME).resolve()
        / "telegram"
    )


class _TelegramAPISender:
    def __init__(self, api: Any) -> None:
        self._api = api

    async def send_text(self, *, chat_ref: str, text: str) -> None:
        from tesseract.integrations.telegram.api import TelegramAPIError
        from tesseract.integrations.telegram.chunker import chunk_for_telegram

        chat_id = int(chat_ref)
        for chunk in chunk_for_telegram(text or ""):
            try:
                await self._api.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode="HTML",
                )
            except TelegramAPIError:
                log.warning(
                    "brief_delivery: TelegramAPI HTML send failed "
                    "for chat=%s; retrying plain",
                    chat_ref,
                )
                await self._api.send_message(
                    chat_id=chat_id,
                    text=_strip_html_tags(chunk),
                )


def _strip_html_tags(text: str) -> str:
    import re
    from html import unescape

    return unescape(re.sub(r"<[^>]+>", "", text))




__all__ = [
    "brief_path",
    "delivery_loop",
    "delivery_time",
    "deliver_now",
    "is_due",
]
