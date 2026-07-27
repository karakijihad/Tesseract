"""AU-10 — notifications REST routes.

Covers:

* ``GET /api/notifications/config`` lists the 8 categories with exempt
  flags + per-channel mute state (yaml union runtime).
* ``POST /api/notifications/mute`` rejects anonymous, accepts
  operator-session bodies, persists to ``outbound-mutes.json``.
* ``GET /api/notifications/rates`` reflects ledger usage so the dashboard
  can show "N/cap".
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.orchestrator.autonomy.outbound import (
    CATEGORIES,
    EXEMPT_CATEGORIES,
    RateLedger,
    read_runtime_mutes,
    write_runtime_mutes,
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    import tesseract.paths
    importlib.reload(tesseract.paths)
    return tmp_path


class _FakeRate:
    def __init__(self, default_per_hour: int = 6, per_category: dict | None = None):
        self.default_per_hour = default_per_hour
        self.per_category = per_category or {}


class _FakeTelegramBlock:
    def __init__(
        self,
        *,
        enabled: bool = True,
        muted_categories: list[str] | None = None,
        outbound_rate: _FakeRate | None = None,
    ):
        self.enabled = enabled
        self.muted_categories = muted_categories or []
        self.outbound_rate = outbound_rate or _FakeRate()


class _FakeChannelsConfig:
    def __init__(self, telegram_block: _FakeTelegramBlock | None = None):
        self.telegram = telegram_block or _FakeTelegramBlock()

    def channel_block(self, name: str):
        if name == "telegram":
            return self.telegram
        return None


async def _make_client(channels_config: _FakeChannelsConfig | None = None) -> TestClient:
    from tesseract.mirror.server.routes import notifications as nroute

    app = web.Application()
    app["server_sessions"] = {}
    if channels_config is not None:
        app["channels_config"] = channels_config
    nroute.register(app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


def _inject_operator_session(app: web.Application, sid: str = "sess_op") -> None:
    fake = SimpleNamespace(
        chat_session=SimpleNamespace(ask_fn=lambda *a, **kw: True),
    )
    app["server_sessions"][sid] = fake


# -- GET /api/notifications/config ---------------------------------------


@pytest.mark.asyncio
async def test_get_config_lists_all_categories():
    cfg = _FakeChannelsConfig()
    client = await _make_client(cfg)
    try:
        resp = await client.get("/api/notifications/config")
        assert resp.status == 200
        body = await resp.json()
        cats = [row["category"] for row in body["categories"]]
        assert set(cats) == set(CATEGORIES)
        exempt = {row["category"] for row in body["categories"] if row["exempt"]}
        assert exempt == set(EXEMPT_CATEGORIES)
        # One channel block for telegram.
        chans = body["channels"]
        assert len(chans) == 1 and chans[0]["name"] == "telegram"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_config_unions_yaml_and_runtime_mutes(_isolated_home: Path):
    cfg = _FakeChannelsConfig(
        telegram_block=_FakeTelegramBlock(muted_categories=["agenda_started"]),
    )
    write_runtime_mutes({"telegram": ["upgrade_applied"]})
    client = await _make_client(cfg)
    try:
        resp = await client.get("/api/notifications/config")
        body = await resp.json()
        tg = body["channels"][0]
        assert tg["muted_yaml"] == ["agenda_started"]
        assert tg["muted_runtime"] == ["upgrade_applied"]
        assert set(tg["muted_effective"]) == {"agenda_started", "upgrade_applied"}
    finally:
        await client.close()


# -- POST /api/notifications/mute ----------------------------------------


@pytest.mark.asyncio
async def test_mute_rejects_anonymous():
    client = await _make_client(_FakeChannelsConfig())
    try:
        resp = await client.post(
            "/api/notifications/mute",
            json={"channel": "telegram", "category": "agenda_started", "muted": True},
        )
        assert resp.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mute_persists_for_authed_operator(_isolated_home: Path):
    cfg = _FakeChannelsConfig()
    client = await _make_client(cfg)
    _inject_operator_session(client.app, "sid-1")
    try:
        resp = await client.post(
            "/api/notifications/mute",
            json={
                "session_id": "sid-1",
                "channel": "telegram",
                "category": "agenda_started",
                "muted": True,
            },
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["muted"] is True
        assert "agenda_started" in body["muted_runtime"]
        assert read_runtime_mutes()["telegram"] == ["agenda_started"]
        # Toggle back off.
        resp2 = await client.post(
            "/api/notifications/mute",
            json={
                "session_id": "sid-1",
                "channel": "telegram",
                "category": "agenda_started",
                "muted": False,
            },
        )
        assert resp2.status == 200
        assert (await resp2.json())["muted_runtime"] == []
        assert read_runtime_mutes() == {}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mute_rejects_exempt_category(_isolated_home: Path):
    """GOVERNANCE §9: exempt categories MUST be unmutable."""
    client = await _make_client(_FakeChannelsConfig())
    _inject_operator_session(client.app, "sid-1")
    try:
        for cat in EXEMPT_CATEGORIES:
            resp = await client.post(
                "/api/notifications/mute",
                json={
                    "session_id": "sid-1",
                    "channel": "telegram",
                    "category": cat,
                    "muted": True,
                },
            )
            assert resp.status == 400, f"{cat} should be unmutable"
            body = await resp.json()
            assert "exempt" in body["error"]
        # Unmute (muted=False) on an exempt cat is a noop, allow it.
        resp_ok = await client.post(
            "/api/notifications/mute",
            json={
                "session_id": "sid-1",
                "channel": "telegram",
                "category": "recovery_summary",
                "muted": False,
            },
        )
        assert resp_ok.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mute_rejects_unknown_category(_isolated_home: Path):
    client = await _make_client(_FakeChannelsConfig())
    _inject_operator_session(client.app, "sid-1")
    try:
        resp = await client.post(
            "/api/notifications/mute",
            json={
                "session_id": "sid-1",
                "channel": "telegram",
                "category": "does_not_exist",
                "muted": True,
            },
        )
        assert resp.status == 400
        body = await resp.json()
        assert "does_not_exist" in body["error"]
    finally:
        await client.close()


# -- GET /api/notifications/rates ----------------------------------------


@pytest.mark.asyncio
async def test_rates_reflect_ledger_usage(_isolated_home: Path):
    cfg = _FakeChannelsConfig(
        telegram_block=_FakeTelegramBlock(
            outbound_rate=_FakeRate(default_per_hour=10),
        ),
    )
    ledger = RateLedger()
    ledger.register("telegram", "agenda_started")
    ledger.register("telegram", "agenda_started")
    ledger.register("telegram", "agenda_blocked")
    client = await _make_client(cfg)

    class _Notifier:
        def __init__(self, ledger):
            self._ledger = ledger

        @property
        def ledger(self):
            return self._ledger

    client.app["outbound_notifier"] = _Notifier(ledger)
    try:
        resp = await client.get("/api/notifications/rates")
        assert resp.status == 200
        body = await resp.json()
        rows = {(r["channel"], r["category"]): r for r in body["rows"]}
        assert rows[("telegram", "agenda_started")]["used_last_hour"] == 2
        assert rows[("telegram", "agenda_blocked")]["used_last_hour"] == 1
        # Cap reflects YAML.
        assert rows[("telegram", "agenda_started")]["cap_per_hour"] == 10
        # Exempt flag flows through.
        assert rows[("telegram", "recovery_summary")]["exempt"] is True
    finally:
        await client.close()
