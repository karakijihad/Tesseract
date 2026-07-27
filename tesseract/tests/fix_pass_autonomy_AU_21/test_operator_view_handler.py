"""AU-21 — WS handler + presence cache + REST."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web

from tesseract.mirror.server.routes import operator_view as ov
from tesseract.orchestrator.autonomy.event_bus import AutonomyEventBus
from tesseract.orchestrator.autonomy.models import AgendaSource
from tesseract.orchestrator.autonomy.publishers import set_active_bus


def _session(session_id: str = "s-1") -> SimpleNamespace:
    return SimpleNamespace(session_id=session_id)


@pytest.fixture
def app() -> web.Application:
    return web.Application()


@pytest.fixture
def bus() -> AutonomyEventBus:
    b = AutonomyEventBus()
    set_active_bus(b)
    try:
        yield b
    finally:
        set_active_bus(None)


@pytest.mark.asyncio
async def test_handler_rejects_unknown_view(app, bus) -> None:
    await ov.handle_view_snapshot(app, _session(), {"view": "not-a-view", "view_state": {}})
    assert ov.get_presence(app) is None
    assert bus.peek(AgendaSource.OPERATOR_VIEW) == []


@pytest.mark.asyncio
async def test_handler_rejects_non_dict_data(app, bus) -> None:
    await ov.handle_view_snapshot(app, _session(), None)
    await ov.handle_view_snapshot(app, _session(), "not-a-dict")  # type: ignore[arg-type]
    assert ov.get_presence(app) is None


@pytest.mark.asyncio
async def test_handler_updates_presence_and_publishes(app, bus) -> None:
    await ov.handle_view_snapshot(
        app, _session(), {"view": "autonomy", "view_state": {"agenda_count": 4}}
    )
    presence = ov.get_presence(app)
    assert presence is not None
    assert presence["view"] == "autonomy"
    assert presence["view_state"] == {"agenda_count": 4}
    events = bus.peek(AgendaSource.OPERATOR_VIEW)
    assert len(events) == 1
    assert events[0].payload["view"] == "autonomy"
    assert events[0].payload["long_dwell"] is False
    assert events[0].payload["repeat_switch"] is False


@pytest.mark.asyncio
async def test_handler_redacts_secrets_server_side(app, bus) -> None:
    await ov.handle_view_snapshot(
        app,
        _session(),
        {
            "view": "settings",
            "view_state": {
                "open_sections": ["voice"],
                "telegram_bot_token": "leaked-secret-abc",
                "nested": {"api_key": "kkk", "ok": True},
            },
        },
    )
    presence = ov.get_presence(app)
    assert presence is not None
    state = presence["view_state"]
    assert state["telegram_bot_token"] == "[redacted]"
    assert state["nested"]["api_key"] == "[redacted]"
    assert state["nested"]["ok"] is True
    assert state["open_sections"] == ["voice"]


@pytest.mark.asyncio
async def test_long_dwell_threshold_fires_on_switch(app, bus, monkeypatch) -> None:
    """After ≥LONG_DWELL_SECONDS on view A, switching to B stamps long_dwell."""
    s = _session()
    # First snapshot: enter autonomy.
    await ov.handle_view_snapshot(app, s, {"view": "autonomy", "view_state": {}})
    # Rewind the dwell-start by 400s to simulate a long sit.
    dwell_map = app[ov._DWELL_KEY]
    prev_view, prev_ts = dwell_map[s.session_id]
    dwell_map[s.session_id] = (prev_view, prev_ts - 400.0)
    # Now switch to terminal.
    await ov.handle_view_snapshot(app, s, {"view": "terminal", "view_state": {}})
    events = bus.peek(AgendaSource.OPERATOR_VIEW)
    assert len(events) == 2
    last = events[-1].payload
    assert last["long_dwell"] is True
    assert last["prev_view"] == "autonomy"
    assert last["dwell_seconds"] >= 400.0


@pytest.mark.asyncio
async def test_repeat_switch_threshold_fires_on_third_visit(app, bus) -> None:
    """Switch-IN counter ≥REPEAT_SWITCH_N for a view stamps repeat_switch."""
    s = _session()
    for view in ["autonomy", "terminal", "autonomy", "terminal", "autonomy", "terminal"]:
        await ov.handle_view_snapshot(app, s, {"view": view, "view_state": {}})
    events = bus.peek(AgendaSource.OPERATOR_VIEW)
    # Find the third switch INTO 'terminal' (after autonomy↔terminal cycles).
    terminal_repeat = [
        e for e in events
        if e.payload["view"] == "terminal" and e.payload["repeat_switch"]
    ]
    assert terminal_repeat, "repeat_switch never fired"
    assert terminal_repeat[0].payload["switch_count_today"] >= ov.REPEAT_SWITCH_N


@pytest.mark.asyncio
async def test_dwell_does_not_fire_within_threshold(app, bus) -> None:
    s = _session()
    await ov.handle_view_snapshot(app, s, {"view": "autonomy", "view_state": {}})
    await ov.handle_view_snapshot(app, s, {"view": "terminal", "view_state": {}})
    last = bus.peek(AgendaSource.OPERATOR_VIEW)[-1].payload
    assert last["long_dwell"] is False


@pytest.mark.asyncio
async def test_rest_get_presence(client) -> None:
    """GET /api/operator/presence returns the cache (or null when empty)."""
    resp = await client.get("/api/operator/presence")
    body = await resp.json()
    assert resp.status == 200
    assert "presence" in body
    assert body["presence"] is None

    # Seed presence directly.
    client.server.app[ov.PRESENCE_KEY] = {
        "session_id": "s-1",
        "view": "autonomy",
        "view_state": {"a": 1},
        "since_ts": "2026-05-19T00:00:00+00:00",
    }
    resp = await client.get("/api/operator/presence")
    body = await resp.json()
    assert body["presence"]["view"] == "autonomy"
    assert body["presence"]["view_state"] == {"a": 1}


@pytest.mark.asyncio
async def test_prune_switch_counts_drops_old_dates(app) -> None:
    counts: dict[tuple[str, str], int] = {
        ("2026-01-01", "autonomy"): 5,
        ("2026-05-19", "terminal"): 2,
    }
    ov._prune_switch_counts(counts, today_iso="2026-05-19")
    assert ("2026-01-01", "autonomy") not in counts
    assert ("2026-05-19", "terminal") in counts


@pytest.mark.asyncio
async def test_prune_switch_counts_retention_boundary(app) -> None:
    """SWITCH_COUNT_RETENTION_DAYS=2 → keep today + yesterday, drop age=2."""
    counts: dict[tuple[str, str], int] = {
        ("2026-05-19", "today_view"): 1,        # age=0 keep
        ("2026-05-18", "yesterday_view"): 1,    # age=1 keep
        ("2026-05-17", "two_days_ago"): 1,      # age=2 drop
        ("2026-05-16", "three_days_ago"): 1,    # age=3 drop
    }
    ov._prune_switch_counts(counts, today_iso="2026-05-19")
    assert ("2026-05-19", "today_view") in counts
    assert ("2026-05-18", "yesterday_view") in counts
    assert ("2026-05-17", "two_days_ago") not in counts
    assert ("2026-05-16", "three_days_ago") not in counts


@pytest.mark.asyncio
async def test_prune_switch_counts_drops_malformed_dates(app) -> None:
    counts: dict[tuple[str, str], int] = {
        ("not-a-date", "broken"): 1,
        ("2026-05-19", "today"): 1,
    }
    ov._prune_switch_counts(counts, today_iso="2026-05-19")
    assert ("not-a-date", "broken") not in counts
    assert ("2026-05-19", "today") in counts


@pytest.mark.asyncio
async def test_handler_drops_when_no_bus_registered(app) -> None:
    """publish_to_bus is a silent no-op without an active bus; presence still updates."""
    # No fixture → no set_active_bus call.
    set_active_bus(None)
    await ov.handle_view_snapshot(app, _session(), {"view": "autonomy", "view_state": {}})
    assert ov.get_presence(app) is not None
