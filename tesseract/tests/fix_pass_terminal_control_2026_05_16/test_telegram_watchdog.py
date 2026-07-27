"""Phase D — Telegram watchdog state-transition pinning.

The watchdog is a separate process from the Mirror backend; it polls
/api/health and fires Telegram notifications on up/down transitions
exactly once each. Tests pin:

- WatchdogConfig.from_env: required keys, fallback to TELEGRAM_ALLOWED_CHAT_IDS
- probe_health: 2xx true, anything else / network error false
- run_loop state machine: down requires grace_probes consecutive
  failures, up flips back on first success, transitions fire exactly
  one Telegram POST each
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from tesseract.integrations.telegram.watchdog import (
    WatchdogConfig,
    _load_env,
    format_transition,
    post_telegram,
    probe_health,
    run_loop,
)


@pytest.fixture(autouse=True)
def _isolate_log_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))


# ─── config parsing ───────────────────────────────────────────────


def test_config_requires_bot_token(monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        WatchdogConfig.from_env()


def test_config_requires_chat_id(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.delenv("TELEGRAM_WATCHDOG_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    with pytest.raises(RuntimeError, match="CHAT_ID"):
        WatchdogConfig.from_env()


def test_config_falls_back_to_first_allowed_chat_id(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.delenv("TELEGRAM_WATCHDOG_CHAT_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "12345,67890")
    cfg = WatchdogConfig.from_env()
    assert cfg.chat_id == 12345


def test_config_explicit_chat_id_wins(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_WATCHDOG_CHAT_ID", "99")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "12345")
    cfg = WatchdogConfig.from_env()
    assert cfg.chat_id == 99


def test_config_defaults_are_sane(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_WATCHDOG_CHAT_ID", "1")
    for k in (
        "TELEGRAM_WATCHDOG_HEALTH_URL",
        "TELEGRAM_WATCHDOG_INTERVAL_S",
        "TELEGRAM_WATCHDOG_TIMEOUT_S",
        "TELEGRAM_WATCHDOG_GRACE_PROBES",
    ):
        monkeypatch.delenv(k, raising=False)
    cfg = WatchdogConfig.from_env()
    assert cfg.health_url == "http://127.0.0.1:8000/api/health"
    assert cfg.interval_s == 30.0
    assert cfg.timeout_s == 5.0
    assert cfg.grace_probes == 2


def test_config_bad_chat_id_raises(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_WATCHDOG_CHAT_ID", "not-an-int")
    with pytest.raises(RuntimeError, match="must be an int"):
        WatchdogConfig.from_env()


# ─── .env self-load (Task Scheduler path) ────────────────────────


def test_load_env_populates_token_from_dotenv(tmp_path: Path, monkeypatch) -> None:
    """A logon scheduled task inherits the OS user env, not the repo .env.
    _load_env must read tesseract/.env so from_env finds the token even when
    it is absent from the process environment."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    (tmp_path / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=from-dotenv-file\n", encoding="utf-8"
    )
    import tesseract.paths as paths
    monkeypatch.setattr(paths, "TESSERACT_DIR", tmp_path)

    _load_env()

    assert os.environ.get("TELEGRAM_BOT_TOKEN") == "from-dotenv-file"


def test_load_env_does_not_override_os_env(tmp_path: Path, monkeypatch) -> None:
    """An explicit OS-level token wins over .env (load_dotenv no-override)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-os-env")
    (tmp_path / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=from-dotenv-file\n", encoding="utf-8"
    )
    import tesseract.paths as paths
    monkeypatch.setattr(paths, "TESSERACT_DIR", tmp_path)

    _load_env()

    assert os.environ.get("TELEGRAM_BOT_TOKEN") == "from-os-env"


# ─── probe_health ────────────────────────────────────────────────


def _client_with(transport: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=transport)


def test_probe_health_2xx_is_up() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))
    with _client_with(transport) as client:
        assert probe_health(client, "http://test/api/health", 1.0) is True


def test_probe_health_5xx_is_down() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(503))
    with _client_with(transport) as client:
        assert probe_health(client, "http://test/api/health", 1.0) is False


def test_probe_health_network_error_is_down() -> None:
    def _raise(_req):
        raise httpx.ConnectError("refused")
    transport = httpx.MockTransport(_raise)
    with _client_with(transport) as client:
        assert probe_health(client, "http://test/api/health", 1.0) is False


# ─── format_transition ────────────────────────────────────────────


def test_format_down_message_includes_warning() -> None:
    from datetime import datetime
    msg = format_transition("down", datetime(2026, 5, 16, 13, 47, 2))
    assert "offline" in msg.lower()
    assert "13:47:02" in msg
    assert "won't be received" in msg


def test_format_up_message_is_short_and_dated() -> None:
    from datetime import datetime
    msg = format_transition("up", datetime(2026, 5, 16, 13, 51, 18))
    assert "online" in msg.lower()
    assert "13:51:18" in msg


# ─── run_loop state machine ───────────────────────────────────────


class _ScriptedProbeClient:
    """httpx.MockTransport-backed client that cycles through a script of
    up/down responses for health probes and records Telegram POSTs.

    The single client instance is materialised eagerly so the
    monkeypatch can return it via a lambda without recursing back into
    the patched ``httpx.Client`` constructor.
    """

    def __init__(self, *, health_script: list[bool]) -> None:
        self._health_script = list(health_script)
        self.telegram_posts: list[dict[str, str]] = []

        def _handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "api.telegram.org" in url:
                body = req.content.decode("utf-8")
                fields = dict(
                    pair.split("=", 1)
                    for pair in body.split("&")
                    if "=" in pair
                )
                self.telegram_posts.append(fields)
                return httpx.Response(200, json={"ok": True})
            up = self._health_script.pop(0) if self._health_script else True
            return httpx.Response(200 if up else 503)

        self._transport = httpx.MockTransport(_handler)
        self._client = httpx.Client(transport=self._transport)
        # Disable __exit__'s real close so the loop's `with httpx.Client()
        # as client:` doesn't tear down our shared instance between
        # iterations (run_loop creates the client once per call, but
        # belt-and-braces).
        self._client.__exit__ = lambda *a, **k: None  # type: ignore[method-assign]

    @property
    def client(self) -> httpx.Client:
        return self._client


def _cfg(monkeypatch) -> WatchdogConfig:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_WATCHDOG_CHAT_ID", "42")
    monkeypatch.setenv("TELEGRAM_WATCHDOG_INTERVAL_S", "0")  # tight loop in tests
    monkeypatch.setenv("TELEGRAM_WATCHDOG_GRACE_PROBES", "2")
    return WatchdogConfig.from_env()


def test_run_loop_no_transition_no_post(monkeypatch) -> None:
    """All probes up → no Telegram traffic at all."""
    cfg = _cfg(monkeypatch)
    scripted = _ScriptedProbeClient(health_script=[True, True, True])
    import tesseract.integrations.telegram.watchdog as wd
    monkeypatch.setattr(
        wd.httpx, "Client", lambda *a, **k: scripted.client,
    )
    probes = run_loop(cfg, stop_after=3)
    assert probes == 3
    assert scripted.telegram_posts == []


def test_run_loop_down_requires_grace_probes(monkeypatch) -> None:
    """First failure alone doesn't fire down — grace_probes=2 means we
    need two consecutive failures."""
    cfg = _cfg(monkeypatch)
    scripted = _ScriptedProbeClient(health_script=[True, False, True, True])
    import tesseract.integrations.telegram.watchdog as wd
    monkeypatch.setattr(wd.httpx, "Client", lambda *a, **k: scripted.client)
    run_loop(cfg, stop_after=4)
    assert scripted.telegram_posts == []


def test_run_loop_down_then_up_fires_once_each(monkeypatch) -> None:
    cfg = _cfg(monkeypatch)
    # up, fail, fail (→DOWN), fail (still DOWN, no extra post), up (→UP)
    scripted = _ScriptedProbeClient(health_script=[True, False, False, False, True])
    import tesseract.integrations.telegram.watchdog as wd
    monkeypatch.setattr(wd.httpx, "Client", lambda *a, **k: scripted.client)
    run_loop(cfg, stop_after=5)
    assert len(scripted.telegram_posts) == 2
    # Body fields use URL-encoded form encoding; check the text key.
    down_text = scripted.telegram_posts[0].get("text", "")
    up_text = scripted.telegram_posts[1].get("text", "")
    assert "offline" in down_text.lower() or "%F0%9F%94%B4" in down_text  # 🔴 emoji url-encoded
    assert "online" in up_text.lower() or "%F0%9F%9F%A2" in up_text  # 🟢 emoji url-encoded
    # Both posts target the configured chat.
    assert all(p.get("chat_id") == "42" for p in scripted.telegram_posts)


def test_run_loop_first_boot_with_unreachable_backend_pages_after_grace(monkeypatch) -> None:
    """Watchdog starts before backend — after grace_probes, it should
    fire the offline notice once so the operator knows the watchdog
    itself is alive."""
    cfg = _cfg(monkeypatch)
    scripted = _ScriptedProbeClient(health_script=[False, False, False])
    import tesseract.integrations.telegram.watchdog as wd
    monkeypatch.setattr(wd.httpx, "Client", lambda *a, **k: scripted.client)
    run_loop(cfg, stop_after=3)
    assert len(scripted.telegram_posts) == 1
    assert "offline" in scripted.telegram_posts[0].get("text", "").lower() or \
           "%F0%9F%94%B4" in scripted.telegram_posts[0].get("text", "")
