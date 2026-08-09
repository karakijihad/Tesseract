"""Backend health watchdog — Telegram push notifier.

Runs **independently** of the Mirror backend so it survives the
backend crashing or being stopped. Polls a health endpoint at a fixed
interval; on state transition (up→down, down→up) posts exactly one
message to a configured Telegram chat via the bot's `sendMessage` HTTP
endpoint. Steady-state is silent — the operator only hears from the
watchdog when something changed.

Run with:
    python -m tesseract.integrations.telegram.watchdog

Manual-only: launch it yourself alongside the backend when you want
health paging. It is deliberately NOT auto-started (no logon task, no
supervisor sibling) — per operator rule, nothing Tesseract runs unless
explicitly launched.

Environment:
    TELEGRAM_BOT_TOKEN              — bot token (required).
    TELEGRAM_WATCHDOG_CHAT_ID       — chat to notify. If unset, falls
                                       back to the first id in
                                       TELEGRAM_ALLOWED_CHAT_IDS.
    TELEGRAM_WATCHDOG_HEALTH_URL    — default http://127.0.0.1:8000/api/health.
    TELEGRAM_WATCHDOG_INTERVAL_S    — seconds between probes (default 30).
    TELEGRAM_WATCHDOG_TIMEOUT_S     — per-probe timeout (default 5).
    TELEGRAM_WATCHDOG_GRACE_PROBES  — consecutive failures before
                                       declaring DOWN (default 2 — one
                                       transient timeout doesn't page).

The watchdog never uses the bot's update-polling channel, so it does
not compete with the main bridge for Telegram long-polling.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import httpx

log = logging.getLogger("telegram.watchdog")

State = Literal["up", "down"]


@dataclass(frozen=True)
class WatchdogConfig:
    bot_token: str
    chat_id: int
    health_url: str
    interval_s: float
    timeout_s: float
    grace_probes: int

    @classmethod
    def from_env(cls) -> "WatchdogConfig":
        token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN not set — watchdog requires it")
        chat_raw = (os.environ.get("TELEGRAM_WATCHDOG_CHAT_ID") or "").strip()
        if not chat_raw:
            seed = (os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS") or "").strip()
            chat_raw = seed.split(",")[0].strip() if seed else ""
        if not chat_raw:
            raise RuntimeError(
                "TELEGRAM_WATCHDOG_CHAT_ID not set (and TELEGRAM_ALLOWED_CHAT_IDS "
                "empty) — watchdog needs a target chat"
            )
        try:
            chat_id = int(chat_raw)
        except ValueError as exc:
            raise RuntimeError(f"TELEGRAM_WATCHDOG_CHAT_ID must be an int, got {chat_raw!r}") from exc
        return cls(
            bot_token=token,
            chat_id=chat_id,
            health_url=os.environ.get(
                "TELEGRAM_WATCHDOG_HEALTH_URL",
                "http://127.0.0.1:8000/api/health",
            ),
            interval_s=float(os.environ.get("TELEGRAM_WATCHDOG_INTERVAL_S", "30")),
            timeout_s=float(os.environ.get("TELEGRAM_WATCHDOG_TIMEOUT_S", "5")),
            grace_probes=int(os.environ.get("TELEGRAM_WATCHDOG_GRACE_PROBES", "2")),
        )


def probe_health(client: httpx.Client, url: str, timeout_s: float) -> bool:
    """Return True iff the health endpoint answers 2xx within timeout."""
    try:
        resp = client.get(url, timeout=timeout_s)
        return 200 <= resp.status_code < 300
    except (httpx.HTTPError, OSError):
        return False


def post_telegram(client: httpx.Client, cfg: WatchdogConfig, text: str) -> bool:
    """Post a plain-text message via the bot API. Returns True on 2xx.

    `disable_notification` is intentionally omitted — the form-encoded
    string `"false"` is non-empty and some Telegram Bot API clients
    treat it as truthy, silently muting the alert. Omitting the field
    leaves it at the default (notify), which is the only correct
    behaviour for a backend-offline alert.
    """
    api = f"https://api.telegram.org/bot{cfg.bot_token}/sendMessage"
    try:
        resp = client.post(
            api,
            data={"chat_id": cfg.chat_id, "text": text},
            timeout=cfg.timeout_s + 5.0,
        )
        if 200 <= resp.status_code < 300:
            return True
        log.warning("watchdog: telegram sendMessage returned %d: %s", resp.status_code, resp.text[:200])
        return False
    except (httpx.HTTPError, OSError) as exc:
        log.warning("watchdog: telegram POST failed: %s", exc)
        return False


def format_transition(new_state: State, when: datetime) -> str:
    stamp = when.strftime("%H:%M:%S")
    if new_state == "down":
        return (
            f"🔴 the assistant backend offline — {stamp}.\n"
            "Messages sent now won't be received until I'm back up."
        )
    return f"🟢 the assistant backend back online — {stamp}."


def run_loop(cfg: WatchdogConfig, *, stop_after: int | None = None) -> int:
    """Main loop. ``stop_after`` caps the iteration count for tests; the
    production caller leaves it None and relies on signals to stop.
    Returns the number of probes performed.
    """
    state: State | None = None
    consecutive_failures = 0
    probes = 0
    with httpx.Client() as client:
        while True:
            if stop_after is not None and probes >= stop_after:
                return probes
            up = probe_health(client, cfg.health_url, cfg.timeout_s)
            probes += 1
            if up:
                consecutive_failures = 0
                if state != "up":
                    if state is not None:
                        post_telegram(client, cfg, format_transition("up", datetime.now()))
                    state = "up"
                    log.info("watchdog: backend UP")
            else:
                consecutive_failures += 1
                if consecutive_failures >= cfg.grace_probes and state != "down":
                    # First boot scenario — if state is None and we've
                    # never seen UP, still fire the down notice once so
                    # the operator knows the watchdog itself is alive
                    # and the backend isn't reachable.
                    post_telegram(client, cfg, format_transition("down", datetime.now()))
                    state = "down"
                    log.info("watchdog: backend DOWN")
            try:
                time.sleep(cfg.interval_s)
            except KeyboardInterrupt:
                return probes


def _load_env() -> None:
    """Load the per-user ``.env`` (under ``TESSERACT_HOME``) so the watchdog
    is self-sufficient.

    Under Task Scheduler the watchdog launches at logon and inherits the OS
    user environment — NOT the ``.env`` where ``TELEGRAM_BOT_TOKEN`` lives.
    Without this, ``WatchdogConfig.from_env`` would raise "token not set" even
    though the token is configured. ``load_dotenv`` does not override variables
    already present in the environment, so an explicit OS-level override still
    wins. Best-effort: if it fails, ``from_env`` still raises a clear message.
    """
    try:
        from dotenv import load_dotenv

        from tesseract.paths import home_dir

        load_dotenv(home_dir() / ".env")
    except Exception as exc:  # noqa: BLE001
        log.warning("watchdog: .env load failed (%s); relying on OS environment", exc)


def _install_signal_handlers() -> None:
    """Graceful shutdown on Ctrl-C / SIGTERM. Windows lacks SIGTERM but
    has SIGBREAK; install both where available."""

    def _bye(_signo, _frame) -> None:  # type: ignore[no-untyped-def]
        log.info("watchdog: signal received — exiting")
        sys.exit(0)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _bye)
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    _ = argv
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _install_signal_handlers()
    _load_env()
    try:
        cfg = WatchdogConfig.from_env()
    except RuntimeError as exc:
        log.error("%s", exc)
        return 2
    log.info(
        "watchdog: starting — url=%s interval=%.0fs grace=%d chat=%d",
        cfg.health_url, cfg.interval_s, cfg.grace_probes, cfg.chat_id,
    )
    run_loop(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
