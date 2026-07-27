from __future__ import annotations

import json

import pytest

from tesseract.scheduler.tasks.daily_brief import (
    _broadcast_brief_ready,
    _build_brief_push_subscriber,
)


class _Push:
    def __init__(self) -> None:
        self.calls = 0

    async def handle(self) -> dict[str, int]:
        self.calls += 1
        return {"sent": 1, "skipped": 0, "errors": 0}


@pytest.mark.asyncio
async def test_scheduler_daily_brief_pushes_without_mirror_sessions() -> None:
    push = _Push()
    app = {"server_sessions": {}, "brief_push_subscriber": push}

    await _broadcast_brief_ready(
        app,
        date="2026-05-15",
        path="memory-store/daily/briefs/2026-05-15.md",
        summary="Daily Brief",
    )

    assert push.calls == 1


@pytest.mark.asyncio
async def test_scheduler_daily_brief_keeps_route_path_with_mirror_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    push = _Push()
    calls: list[dict[str, str]] = []
    app = {"server_sessions": {"s1": object()}, "brief_push_subscriber": push}

    async def _fake_broadcast(app_arg, *, date: str, path: str, summary: str) -> None:
        assert app_arg is app
        calls.append({"date": date, "path": path, "summary": summary})

    monkeypatch.setattr(
        "tesseract.mirror.server.routes.brief.broadcast_daily_brief_ready",
        _fake_broadcast,
    )

    await _broadcast_brief_ready(
        app,
        date="2026-05-15",
        path="memory-store/daily/briefs/2026-05-15.md",
        summary="Daily Brief",
    )

    assert calls == [
        {
            "date": "2026-05-15",
            "path": "memory-store/daily/briefs/2026-05-15.md",
            "summary": "Daily Brief",
        }
    ]
    assert push.calls == 0


class _PollState:
    def __init__(self, user_tier: dict[str, str]) -> None:
        self.user_tier = user_tier


class _BridgeState:
    def __init__(self) -> None:
        self.allowlist_path = "/tmp/allowlist.json"
        self.poll_state = _PollState({"111": "operator"})


class _Bridge:
    def __init__(self) -> None:
        self._state = _BridgeState()


@pytest.mark.asyncio
async def test_scheduler_builds_subscriber_on_demand_when_unwired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handled: list[bool] = []

    class _Sub:
        def __init__(self, **_: object) -> None:
            pass

        async def handle(self) -> dict[str, int]:
            handled.append(True)
            return {"sent": 1, "skipped": 0, "errors": 0}

    monkeypatch.setattr(
        "tesseract.integrations.telegram.brief_push.TelegramBriefPushSubscriber",
        _Sub,
    )
    monkeypatch.setattr(
        "tesseract.integrations.telegram.state.load_allowlist",
        lambda _path: object(),
    )

    app = {
        "server_sessions": {},
        "telegram_bridge": _Bridge(),
        "workspace_event_store": object(),
        "channels_config": object(),
    }

    await _broadcast_brief_ready(
        app,
        date="2026-05-15",
        path="memory-store/daily/briefs/2026-05-15.md",
        summary="Daily Brief",
    )

    assert handled == [True]


def test_build_subscriber_returns_none_without_bridge() -> None:
    app = {"workspace_event_store": object()}
    assert _build_brief_push_subscriber(app) is None


class _TelegramConfig:
    brief_push = True


class _ChannelsConfig:
    telegram = _TelegramConfig()


class _BriefEvent:
    def __init__(self, payload: dict) -> None:
        self.payload = payload


class _BriefEventStore:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def list_events(self, *, kinds, limit=5):
        return [_BriefEvent(self._payload)]


def _workspace_payload() -> dict:
    return {
        "kind": "daily_brief",
        "date": "2026-05-15",
        "sections": {
            "yesterday_in_tesseract": "TARS shipped the daily brief push.",
            "yesterday_with_you": "",
            "what_i_learned": "",
            "vault": [],
            "world": {"tech": [], "science": [], "politics": []},
        },
    }


@pytest.mark.asyncio
async def test_scheduler_falls_back_to_telegram_api_without_bridge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    sent: list[dict[str, object]] = []

    class _API:
        def __init__(self, token: str) -> None:
            self.token = token

        async def send_message(self, **kwargs):
            sent.append({"token": self.token, **kwargs})
            return {"message_id": 123}

        async def aclose(self) -> None:
            pass

    state_dir = tmp_path / "telegram"
    state_dir.mkdir()
    (state_dir / "allowlist.json").write_text(
        json.dumps({"chat_ids": [111], "pending": [], "blocked": []}),
        encoding="utf-8",
    )
    (state_dir / "state.json").write_text(
        json.dumps({"user_tier": {"111": "operator"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    monkeypatch.setattr("tesseract.integrations.telegram.api.TelegramAPI", _API)

    app = {
        "server_sessions": {},
        "workspace_event_store": _BriefEventStore(_workspace_payload()),
        "channels_config": _ChannelsConfig(),
    }

    await _broadcast_brief_ready(
        app,
        date="2026-05-15",
        path="memory-store/daily/briefs/2026-05-15.md",
        summary="Daily Brief",
    )

    assert len(sent) == 1
    assert sent[0]["token"] == "secret-token"
    assert sent[0]["chat_id"] == 111
    assert sent[0]["parse_mode"] == "HTML"
    assert "TARS shipped the daily brief push" in str(sent[0]["text"])


@pytest.mark.asyncio
async def test_scheduler_falls_back_to_telegram_api_with_mirror_sessions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    sent: list[dict[str, object]] = []
    route_calls: list[str] = []

    class _API:
        def __init__(self, token: str) -> None:
            self.token = token

        async def send_message(self, **kwargs):
            sent.append({"token": self.token, **kwargs})
            return {"message_id": 123}

        async def aclose(self) -> None:
            pass

    async def _fake_broadcast(app_arg, *, date: str, path: str, summary: str) -> None:
        route_calls.append(date)

    state_dir = tmp_path / "telegram"
    state_dir.mkdir()
    (state_dir / "allowlist.json").write_text(
        json.dumps({"chat_ids": [111], "pending": [], "blocked": []}),
        encoding="utf-8",
    )
    (state_dir / "state.json").write_text(
        json.dumps({"user_tier": {"111": "operator"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    monkeypatch.setattr("tesseract.integrations.telegram.api.TelegramAPI", _API)
    monkeypatch.setattr(
        "tesseract.mirror.server.routes.brief.broadcast_daily_brief_ready",
        _fake_broadcast,
    )

    app = {
        "server_sessions": {"s1": object()},
        "workspace_event_store": _BriefEventStore(_workspace_payload()),
        "channels_config": _ChannelsConfig(),
    }

    await _broadcast_brief_ready(
        app,
        date="2026-05-15",
        path="memory-store/daily/briefs/2026-05-15.md",
        summary="Daily Brief",
    )

    assert route_calls == ["2026-05-15"]
    assert len(sent) == 1
    assert sent[0]["chat_id"] == 111


@pytest.mark.asyncio
async def test_scheduler_telegram_api_fallback_requires_matching_brief_date(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    sent: list[dict[str, object]] = []

    class _API:
        def __init__(self, token: str) -> None:
            self.token = token

        async def send_message(self, **kwargs):
            sent.append({"token": self.token, **kwargs})
            return {"message_id": 123}

        async def aclose(self) -> None:
            pass

    stale_payload = _workspace_payload()
    stale_payload["date"] = "2026-05-14"
    state_dir = tmp_path / "telegram"
    state_dir.mkdir()
    (state_dir / "allowlist.json").write_text(
        json.dumps({"chat_ids": [111], "pending": [], "blocked": []}),
        encoding="utf-8",
    )
    (state_dir / "state.json").write_text(
        json.dumps({"user_tier": {"111": "operator"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    monkeypatch.setattr("tesseract.integrations.telegram.api.TelegramAPI", _API)

    app = {
        "server_sessions": {},
        "workspace_event_store": _BriefEventStore(stale_payload),
        "channels_config": _ChannelsConfig(),
    }

    await _broadcast_brief_ready(
        app,
        date="2026-05-15",
        path="memory-store/daily/briefs/2026-05-15.md",
        summary="Daily Brief",
    )

    assert sent == []
