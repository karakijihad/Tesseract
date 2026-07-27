"""MO-10-3 §2a/§2e — subscriber lifecycle: disabled, enabled, no bridge,
no payload."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from tesseract.integrations.telegram.brief_push import TelegramBriefPushSubscriber


@dataclass
class _Telegram:
    brief_push: bool = False


@dataclass
class _Cfg:
    telegram: _Telegram = field(default_factory=_Telegram)


@dataclass
class _Allowlist:
    chat_ids: set = field(default_factory=set)
    pending: dict = field(default_factory=dict)
    blocked: set = field(default_factory=set)


class _Bridge:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, *, chat_ref: str, text: str) -> None:
        self.sent.append((chat_ref, text))


class _Event:
    def __init__(self, kind: str, payload: dict) -> None:
        self.kind = kind
        self.payload = payload


class _Store:
    def __init__(self, events: list[_Event]) -> None:
        self._events = events

    def list_events(self, *, kinds, limit=5):
        return [e for e in self._events if e.kind in kinds][:limit]


def _payload() -> dict:
    return {
        "kind": "daily_brief",
        "date": "2026-05-15",
        "sections": {
            "yesterday_in_tesseract": "TARS shipped MO-10-3.",
            "yesterday_with_you": "",
            "what_i_learned": "",
            "vault": [],
            "world": {"tech": [], "science": [], "politics": []},
        },
    }


def test_subscriber_disabled_returns_no_send():
    cfg = _Cfg(telegram=_Telegram(brief_push=False))
    bridge = _Bridge()
    store = _Store([_Event("daily_brief", _payload())])
    sub = TelegramBriefPushSubscriber(
        bridge=bridge,
        event_store=store,
        config_loader=lambda: cfg,
        allowlist_loader=lambda: _Allowlist(chat_ids={111}),
        user_tier_loader=lambda: {"111": "operator"},
    )
    res = asyncio.run(sub.handle())
    assert res["sent"] == 0
    assert res.get("reason") == "disabled"


def test_subscriber_enabled_sends_to_operator():
    cfg = _Cfg(telegram=_Telegram(brief_push=True))
    bridge = _Bridge()
    store = _Store([_Event("daily_brief", _payload())])
    sub = TelegramBriefPushSubscriber(
        bridge=bridge,
        event_store=store,
        config_loader=lambda: cfg,
        allowlist_loader=lambda: _Allowlist(chat_ids={111}),
        user_tier_loader=lambda: {"111": "operator"},
    )
    res = asyncio.run(sub.handle())
    assert res["sent"] == 1
    assert "TESSERACT" in bridge.sent[0][1]


def test_subscriber_no_bridge_is_safe():
    cfg = _Cfg(telegram=_Telegram(brief_push=True))
    sub = TelegramBriefPushSubscriber(
        bridge=None,
        event_store=_Store([_Event("daily_brief", _payload())]),
        config_loader=lambda: cfg,
        allowlist_loader=lambda: _Allowlist(),
    )
    res = asyncio.run(sub.handle())
    assert res["sent"] == 0
    assert res.get("reason") == "no_bridge"


def test_subscriber_no_payload_logs_and_skips():
    cfg = _Cfg(telegram=_Telegram(brief_push=True))
    bridge = _Bridge()
    sub = TelegramBriefPushSubscriber(
        bridge=bridge,
        event_store=_Store([]),
        config_loader=lambda: cfg,
        allowlist_loader=lambda: _Allowlist(chat_ids={111}),
        user_tier_loader=lambda: {"111": "operator"},
    )
    res = asyncio.run(sub.handle())
    assert res["sent"] == 0
    assert res.get("reason") == "no_payload"
    assert bridge.sent == []
