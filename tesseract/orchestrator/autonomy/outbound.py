"""OutboundNotifier — AU-10.

Single notify path for autonomous → operator pings over Telegram (and any
future channel that implements ``ChannelAdapter``). Replaces the two
hand-rolled rate-cap-exempt closures (Governor + UpgradeManager restart)
with a unified surface that:

* formats per-category messages via small inline templates,
* honours per-(category, channel) sliding-window rate caps loaded from
  ``channels.yaml::<channel>.outbound_rate``,
* skips muted categories (union of ``channels.yaml::<channel>.muted_categories``
  and the dashboard-editable ``<HOME>/runtime/outbound-mutes.json``),
* lets EXEMPT categories (``recovery_summary``, ``crash_storm_latched``,
  ``awaiting_operator``) bypass the cap entirely per GOVERNANCE §9 + the
  AU-10 phase doc.

The notifier dispatches via the same ``send_to_operators`` helper the
existing rate-cap-exempt paths already use, so allowlist + tier semantics
do not diverge between exempt and capped categories.

Durable state lives at ``<TESSERACT_HOME>/runtime/outbound-rates.json``
(sliding window timestamps per ``(channel, category)``). Path resolution
is call-time so tests routing through ``monkeypatch.setenv("TESSERACT_HOME",
tmp_path)`` keep production runtime untouched.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from tesseract.paths import TESSERACT_HOME, runtime_dir

log = logging.getLogger(__name__)


NotificationCategory = Literal[
    "agenda_started",
    "agenda_blocked",
    "awaiting_operator",
    "recovery_summary",
    "governor_pause",
    "upgrade_restarting",
    "upgrade_applied",
    "crash_storm_latched",
    "runtime_report",
]

CATEGORIES: tuple[NotificationCategory, ...] = (
    "agenda_started",
    "agenda_blocked",
    "awaiting_operator",
    "recovery_summary",
    "governor_pause",
    "upgrade_restarting",
    "upgrade_applied",
    "crash_storm_latched",
    "runtime_report",
)

EXEMPT_CATEGORIES: frozenset[NotificationCategory] = frozenset(
    {"recovery_summary", "crash_storm_latched", "awaiting_operator"}
)

DEFAULT_RATE_PER_HOUR = 6
DEFAULT_WINDOW_SECONDS = 3600
MAX_MESSAGE_CHARS = 512
# Every other category is a ping about one event and 512 characters is more
# than it needs. The runtime report is a list — its findings ARE its content,
# and three of ten reached the operator while seven were unreachable from the
# message. Telegram accepts 4096 per message; the rest is headroom for the
# HTML the template adds around each line.
RUNTIME_REPORT_MAX_CHARS = 3500


def _home() -> Path:
    override = os.environ.get("TESSERACT_HOME")
    return Path(override).resolve() if override else TESSERACT_HOME


def outbound_rates_path() -> Path:
    return runtime_dir() / "outbound-rates.json"


def outbound_mutes_path() -> Path:
    return runtime_dir() / "outbound-mutes.json"


@dataclass(frozen=True)
class NotifyResult:
    """Return value from :meth:`OutboundNotifier.notify`.

    ``sent`` is the count of operator chat_ids the body actually reached.
    ``skipped`` is the cap/mute/no-bridge no-op shape so callers can log
    *why* a notification was dropped without parsing strings."""

    category: NotificationCategory
    sent: int = 0
    skipped: bool = False
    reason: str = ""
    errors: int = 0


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# -- Rate ledger ----------------------------------------------------------


@dataclass
class RateLedger:
    """Sliding-window ledger keyed by ``(channel, category)``.

    Each key holds a list of UTC ISO timestamps. ``register`` prunes
    entries older than ``window_seconds`` before appending. ``allowed``
    checks the post-prune count against ``cap``. Persistence is whole-file
    atomic-write — the ledger is small (8 categories × a few channels ×
    a handful of timestamps) so JSON serialisation cost stays trivial."""

    window_seconds: int = DEFAULT_WINDOW_SECONDS
    _data: dict[str, list[str]] = field(default_factory=dict)
    _loaded: bool = False

    def _key(self, channel: str, category: NotificationCategory) -> str:
        return f"{channel}::{category}"

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        path = outbound_rates_path()
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.exception("outbound: rates file unreadable; resetting")
            return
        windows = raw.get("windows")
        if isinstance(windows, dict):
            for key, stamps in windows.items():
                if isinstance(key, str) and isinstance(stamps, list):
                    self._data[key] = [str(s) for s in stamps if isinstance(s, str)]

    def _persist(self) -> None:
        _atomic_write_json(
            outbound_rates_path(),
            {"schema": 1, "windows": self._data},
        )

    def _prune(self, key: str, now: datetime) -> list[str]:
        cutoff = now - timedelta(seconds=self.window_seconds)
        kept: list[str] = []
        for stamp in self._data.get(key, ()):
            try:
                ts = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                kept.append(stamp)
        self._data[key] = kept
        return kept

    def count(self, channel: str, category: NotificationCategory, *, now: datetime | None = None) -> int:
        self._load()
        moment = now or datetime.now(timezone.utc)
        kept = self._prune(self._key(channel, category), moment)
        return len(kept)

    def allowed(
        self,
        channel: str,
        category: NotificationCategory,
        cap: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        return self.count(channel, category, now=now) < cap

    def register(
        self,
        channel: str,
        category: NotificationCategory,
        *,
        now: datetime | None = None,
    ) -> None:
        self._load()
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        key = self._key(channel, category)
        self._prune(key, moment)
        self._data[key].append(moment.isoformat())
        self._persist()


# -- Templates ------------------------------------------------------------


def _truncate(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _agenda_chip(context: dict[str, Any]) -> str:
    """Format a short item identifier for the body."""
    item_id = str(context.get("item_id") or context.get("agenda_id") or "").strip()
    goal = str(context.get("goal") or "").strip()
    chip = f"<code>{item_id}</code>" if item_id else ""
    if goal:
        chip = f"{chip} · {goal}" if chip else goal
    return chip


def format_message(category: NotificationCategory, context: dict[str, Any]) -> str:
    """Per-category Telegram-HTML body. ≤512 chars after truncation.

    Templates intentionally avoid jinja: the inputs are small and typed,
    and a python-format helper keeps the templates inspectable without
    a dependency. Reply hints (``<id>:approve`` / ``:deny`` / ``:snooze``)
    are appended for categories that route through the inbound mapper.
    """
    if category == "agenda_started":
        body = f"<b>Started</b> · {_agenda_chip(context)}"
        rationale = str(context.get("rationale") or "").strip()
        if rationale:
            body += f"\n{rationale}"
        return _truncate(body)
    if category == "agenda_blocked":
        body = f"<b>Blocked</b> · {_agenda_chip(context)}"
        reason = str(context.get("reason") or "").strip()
        if reason:
            body += f" · {reason}"
        return _truncate(body)
    if category == "awaiting_operator":
        chip = _agenda_chip(context)
        item_id = str(context.get("item_id") or context.get("agenda_id") or "").strip()
        body = f"<b>Awaiting operator</b> · {chip}"
        gates = context.get("gates")
        if isinstance(gates, list) and gates:
            body += f" · gates: {', '.join(str(g) for g in gates)}"
        if item_id:
            body += (
                f"\nReply <code>{item_id}:approve</code> · "
                f"<code>{item_id}:deny</code> · <code>{item_id}:snooze</code>"
            )
        return _truncate(body)
    if category == "recovery_summary":
        text = str(context.get("text") or "").strip()
        if not text:
            text = "Recovery pass complete."
        return _truncate(text)
    if category == "governor_pause":
        source = str(context.get("source") or "").strip()
        detector = str(context.get("detector") or "").strip()
        reason = str(context.get("reason") or "").strip()
        body = f"<b>Governor</b> · source <code>{source}</code> paused"
        if detector:
            body += f" · {detector}"
        if reason:
            body += f" · {reason}"
        return _truncate(body)
    if category == "upgrade_restarting":
        text = str(context.get("text") or "").strip()
        if text:
            return _truncate(text)
        upgrade_id = str(context.get("upgrade_id") or "").strip()
        klass = str(context.get("class") or "").strip()
        body = "<b>Upgrade</b> · restart required"
        if klass:
            body += f" · {klass}"
        if upgrade_id:
            body += f" · <code>{upgrade_id}</code>"
        return _truncate(body)
    if category == "upgrade_applied":
        upgrade_id = str(context.get("upgrade_id") or "").strip()
        klass = str(context.get("class") or "").strip()
        body = "<b>Upgrade applied</b>"
        if klass:
            body += f" · {klass}"
        if upgrade_id:
            body += f" · <code>{upgrade_id}</code>"
        return _truncate(body)
    if category == "crash_storm_latched":
        body = "<b>Crash storm latched</b> · supervisor refused respawn"
        reason = str(context.get("reason") or "").strip()
        if reason:
            body += f" · {reason}"
        return _truncate(body)
    if category == "runtime_report":
        # The watchman, and only when it found a defect — a quiet runtime
        # sends nothing at all rather than an hourly all-clear.
        lines = [str(line) for line in (context.get("lines") or [])]
        count = int(context.get("defects") or len(lines))
        report_path = str(context.get("report_path") or "").strip()
        head = f"<b>Runtime</b> · {count} thing(s) went wrong"
        # The pointer is reserved before the findings are laid out. A message
        # that drops the link to fit one more finding has lost the only part
        # of itself that reaches the ones it could not carry.
        pointer = f"\n\nFull report: <code>{report_path}</code>" if report_path else ""
        budget = RUNTIME_REPORT_MAX_CHARS - len(head) - len(pointer)
        shown: list[str] = []
        for line in lines:
            entry = f"\n· {line}"
            withheld = len(lines) - len(shown) - 1
            tail = f"\n· …and {withheld} more" if withheld else ""
            if len(entry) + len(tail) > budget:
                break
            budget -= len(entry)
            shown.append(entry)
        body = head + "".join(shown)
        if len(shown) < len(lines):
            body += f"\n· …and {len(lines) - len(shown)} more"
        return _truncate(body + pointer, RUNTIME_REPORT_MAX_CHARS)
    return _truncate(str(context.get("text") or category))


# -- Mute store -----------------------------------------------------------


def read_runtime_mutes() -> dict[str, list[str]]:
    """Read the dashboard-editable mute file. Returns
    ``{channel: [category, …]}``; missing file → empty dict.
    """
    path = outbound_mutes_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.exception("outbound: runtime mutes unreadable; treating as empty")
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for channel, cats in raw.items():
        if isinstance(channel, str) and isinstance(cats, list):
            out[channel] = [str(c) for c in cats if isinstance(c, str)]
    return out


def write_runtime_mutes(mutes: dict[str, list[str]]) -> None:
    payload: dict[str, list[str]] = {}
    for channel, cats in mutes.items():
        payload[str(channel)] = sorted({str(c) for c in cats})
    _atomic_write_json(outbound_mutes_path(), payload)


# -- Notifier -------------------------------------------------------------


BridgeGetter = Callable[[], Any | None]
ChannelsConfigGetter = Callable[[], Any | None]
Sender = Callable[..., Awaitable[dict[str, Any]]]


class OutboundNotifier:
    """Single notify path. Construct once per backend; pass into the
    Governor / UpgradeManager / recovery hook so every outbound path
    shares the same rate ledger + mute logic."""

    def __init__(
        self,
        *,
        bridge_getter: BridgeGetter,
        channels_config_getter: ChannelsConfigGetter,
        sender: Sender | None = None,
        clock: Callable[[], datetime] | None = None,
        ledger: RateLedger | None = None,
    ) -> None:
        self._bridge_getter = bridge_getter
        self._channels_config_getter = channels_config_getter
        self._sender = sender
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ledger = ledger or RateLedger()

    @property
    def ledger(self) -> RateLedger:
        return self._ledger

    def _resolve_sender(self) -> Sender:
        if self._sender is not None:
            return self._sender
        from tesseract.integrations.telegram.brief_push import send_to_operators

        return send_to_operators

    def _channel_block(self, name: str) -> Any | None:
        cfg = self._channels_config_getter() if self._channels_config_getter else None
        if cfg is None:
            return None
        block = getattr(cfg, "channel_block", None)
        if callable(block):
            return block(name)
        return getattr(cfg, name, None)

    def _muted(self, channel_name: str, category: NotificationCategory) -> bool:
        block = self._channel_block(channel_name)
        muted_yaml: list[str] = []
        if block is not None:
            raw = getattr(block, "muted_categories", None)
            if isinstance(raw, (list, tuple)):
                muted_yaml = [str(c) for c in raw]
        runtime = read_runtime_mutes().get(channel_name, [])
        return category in muted_yaml or category in runtime

    def _cap_for(self, channel_name: str, category: NotificationCategory) -> int:
        block = self._channel_block(channel_name)
        if block is None:
            return DEFAULT_RATE_PER_HOUR
        rate_block = getattr(block, "outbound_rate", None)
        if rate_block is None:
            return DEFAULT_RATE_PER_HOUR
        # Per-category override → global default → DEFAULT_RATE_PER_HOUR
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

    async def notify(
        self,
        category: NotificationCategory,
        context: dict[str, Any] | None = None,
        *,
        channel_name: str = "telegram",
    ) -> NotifyResult:
        ctx = dict(context or {})
        bridge = self._bridge_getter()
        if bridge is None:
            return NotifyResult(category=category, skipped=True, reason="no_bridge")

        # Exempt categories bypass mute AND rate cap (GOVERNANCE §9: the
        # operator MUST see crash-storm / recovery / awaiting-operator
        # pings even if a dashboard toggle was flipped by accident).
        if category not in EXEMPT_CATEGORIES and self._muted(channel_name, category):
            return NotifyResult(category=category, skipped=True, reason="muted")

        now = self._clock()
        if category not in EXEMPT_CATEGORIES:
            cap = self._cap_for(channel_name, category)
            if cap <= 0:
                return NotifyResult(category=category, skipped=True, reason="cap_zero")
            if not self._ledger.allowed(channel_name, category, cap, now=now):
                return NotifyResult(category=category, skipped=True, reason="rate_capped")

        text = format_message(category, ctx)
        if not text:
            return NotifyResult(category=category, skipped=True, reason="empty_text")

        sender = self._resolve_sender()
        bridge_state = getattr(bridge, "_state", None)
        allowlist = getattr(bridge_state, "allowlist", None)
        poll_state = getattr(bridge_state, "poll_state", None)
        user_tier = getattr(poll_state, "user_tier", None)
        try:
            result = await sender(
                text,
                bridge=bridge,
                allowlist=allowlist,
                user_tier=user_tier if isinstance(user_tier, dict) else None,
            )
        except Exception:
            log.exception("outbound: sender raised for category=%s", category)
            return NotifyResult(category=category, errors=1, reason="sender_raised")

        sent = int(result.get("sent", 0)) if isinstance(result, dict) else 0
        errors = int(result.get("errors", 0)) if isinstance(result, dict) else 0
        if sent > 0 and category not in EXEMPT_CATEGORIES:
            self._ledger.register(channel_name, category, now=now)
        return NotifyResult(category=category, sent=sent, errors=errors)


__all__ = [
    "CATEGORIES",
    "DEFAULT_RATE_PER_HOUR",
    "DEFAULT_WINDOW_SECONDS",
    "EXEMPT_CATEGORIES",
    "MAX_MESSAGE_CHARS",
    "NotificationCategory",
    "NotifyResult",
    "OutboundNotifier",
    "RateLedger",
    "format_message",
    "outbound_mutes_path",
    "outbound_rates_path",
    "read_runtime_mutes",
    "write_runtime_mutes",
]
