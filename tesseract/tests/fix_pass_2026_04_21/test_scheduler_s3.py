"""Regression suite for scheduler S3 — ObserverIdleJob.

Covers: missing threshold config, no observer, no active session, session
without a last_turn_at, session not yet idle, most-idle session fires exactly
one observer pass, observer exception is wrapped.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tesseract.scheduler.tasks.observer_idle import ObserverIdleJob
from tesseract.scheduler.types import JobContext


NOW = datetime(2026, 4, 21, 15, 0, tzinfo=timezone.utc)


def _ctx(app, *, config: dict | None = None) -> JobContext:
    return JobContext(
        job_name="observer_idle_trigger",
        fired_at=NOW,
        app=app,
        config=config if config is not None else {"idle_threshold_minutes": 15},
    )


def _session(session_id: str, *, last_turn_at, history=None) -> SimpleNamespace:
    return SimpleNamespace(
        session_id=session_id,
        chat_session=SimpleNamespace(history=list(history or [])),
        last_turn_at=last_turn_at,
    )


# ── config guard ──────────────────────────────────────────────────────────


async def test_missing_threshold_key_fails():
    result = await ObserverIdleJob().run(_ctx({"observer": AsyncMock()}, config={}))
    assert result.ok is False
    assert "idle_threshold_minutes" in result.detail


# ── no-op paths ───────────────────────────────────────────────────────────


async def test_no_observer_returns_ok_noop():
    result = await ObserverIdleJob().run(_ctx({}))
    assert result.ok is True
    assert result.detail == "no_observer"


async def test_no_active_session_returns_ok_noop():
    observer = AsyncMock()
    result = await ObserverIdleJob().run(
        _ctx({"observer": observer, "server_sessions": {}})
    )
    assert result.ok is True
    assert result.detail == "no_active_session"
    observer.observe.assert_not_called()


async def test_session_without_last_turn_at_is_not_idle():
    observer = AsyncMock()
    sess = _session("s1", last_turn_at=None)
    result = await ObserverIdleJob().run(
        _ctx({"observer": observer, "server_sessions": {"s1": sess}})
    )
    assert result.ok is True
    assert result.detail == "not_idle"
    observer.observe.assert_not_called()


async def test_not_idle_returns_ok_noop():
    observer = AsyncMock()
    sess = _session(
        "s1",
        last_turn_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    result = await ObserverIdleJob().run(
        _ctx({"observer": observer, "server_sessions": {"s1": sess}})
    )
    assert result.ok is True
    assert result.detail == "not_idle"
    observer.observe.assert_not_called()


# ── firing path ───────────────────────────────────────────────────────────


async def test_idle_session_fires_observer_once():
    observer = AsyncMock()
    observer.observe.return_value = "observation text"
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    sess = _session(
        "s1",
        last_turn_at=datetime.now(timezone.utc) - timedelta(minutes=20),
        history=history,
    )

    result = await ObserverIdleJob().run(
        _ctx({"observer": observer, "server_sessions": {"s1": sess}})
    )

    assert result.ok is True
    assert result.detail.startswith("fired idle_s=")
    assert result.payload["session_id"] == "s1"
    assert result.payload["observation_len"] == len("observation text")
    assert result.payload["threshold_minutes"] == 15

    observer.observe.assert_awaited_once()
    args, kwargs = observer.observe.call_args
    assert args[0] == history
    assert kwargs.get("mode") == "meta"


async def test_most_idle_session_wins_when_multiple_cross_threshold():
    observer = AsyncMock()
    observer.observe.return_value = ""
    now = datetime.now(timezone.utc)
    sessions = {
        "newer": _session("newer", last_turn_at=now - timedelta(minutes=20)),
        "older": _session("older", last_turn_at=now - timedelta(minutes=45)),
    }

    result = await ObserverIdleJob().run(
        _ctx({"observer": observer, "server_sessions": sessions})
    )

    assert result.ok is True
    assert result.payload["session_id"] == "older"
    observer.observe.assert_awaited_once()


async def test_observer_raising_returns_not_ok_wrapped():
    observer = AsyncMock()
    observer.observe.side_effect = RuntimeError("boom")
    sess = _session(
        "s1",
        last_turn_at=datetime.now(timezone.utc) - timedelta(minutes=30),
    )

    result = await ObserverIdleJob().run(
        _ctx({"observer": observer, "server_sessions": {"s1": sess}})
    )

    assert result.ok is False
    assert "unhandled" in result.detail
    assert "RuntimeError" in result.detail
    assert "boom" in result.detail
