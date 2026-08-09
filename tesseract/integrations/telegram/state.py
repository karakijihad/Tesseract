from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from tesseract.paths import TESSERACT_HOME

StatusOverride = Literal["online", "offline"]
_SENT_RING_MAX = 1000

# Cap on per-direction rolling timestamp lists (audit fix m1). 24h of
# busy single-operator chat tops out around a few hundred turns; 10000
# bounds memory at ~300 KB per direction even on a runaway chat.
_ROLLING_TS_MAX = 10_000

# Cap on the per-chat offline inbox (audit fix M1). A chat that floods
# the bridge while offline shouldn't be able to grow state.json without
# bound; oldest entries past the cap are dropped with a logged warning.
_OFFLINE_INBOX_PER_CHAT_MAX = 200


def telegram_state_dir() -> Path:
    root = Path(os.environ.get("TESSERACT_HOME") or TESSERACT_HOME).resolve()
    out = root / "telegram"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, payload: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


@dataclass
class PendingChat:
    chat_id: int
    username: str | None
    first_seen: str

    def to_dict(self) -> dict[str, object]:
        return {
            "chat_id": self.chat_id,
            "username": self.username,
            "first_seen": self.first_seen,
        }


@dataclass
class Allowlist:
    chat_ids: set[int] = field(default_factory=set)
    pending: dict[int, PendingChat] = field(default_factory=dict)
    # Chat IDs the operator has explicitly blocked. Bridge `_handle_message`
    # drops blocked chats before pending-record logic — no reply, no
    # state mutation, no log spam. Round-trips through JSON as a sorted
    # list of ints (back-compat: load tolerates missing key).
    blocked: set[int] = field(default_factory=set)

    def is_allowed(self, chat_id: int) -> bool:
        return chat_id in self.chat_ids

    def is_blocked(self, chat_id: int) -> bool:
        return chat_id in self.blocked

    def record_pending(self, chat_id: int, username: str | None) -> bool:
        if chat_id in self.chat_ids or chat_id in self.pending or chat_id in self.blocked:
            return False
        self.pending[chat_id] = PendingChat(
            chat_id=chat_id,
            username=username,
            first_seen=datetime.now(timezone.utc).isoformat(),
        )
        return True


@dataclass
class OfflineMessage:
    """One inbound message saved while the bridge was in ``offline`` override (audit fix M1).

    Persisted to ``state.json::offline_inbox`` so a Mirror restart between
    flip-offline and flip-online does not lose the queued messages. The
    bridge drains the inbox into a real turn when the operator flips the
    override back to online (or the bridge boots with override=online
    and a non-empty inbox).
    """

    ts: str
    telegram_message_id: int
    text: str
    from_user_id: int | None
    from_username: str | None
    attachments: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "ts": self.ts,
            "telegram_message_id": self.telegram_message_id,
            "text": self.text,
            "from_user_id": self.from_user_id,
            "from_username": self.from_username,
            "attachments": list(self.attachments),
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_within_24h(ts_iso: str, *, now: datetime | None = None) -> bool:
    """Best-effort timestamp parse; malformed entries treat as expired."""
    try:
        when = datetime.fromisoformat(ts_iso)
    except (TypeError, ValueError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    horizon = (now or datetime.now(timezone.utc)) - timedelta(hours=24)
    return when >= horizon


def _prune_24h(ts_list: list[str], *, now: datetime | None = None) -> None:
    """Drop entries older than 24h in-place. ``ts_list`` is mutated."""
    keep_from = 0
    n = len(ts_list)
    for i, ts in enumerate(ts_list):
        if _is_within_24h(ts, now=now):
            keep_from = i
            break
    else:
        keep_from = n
    if keep_from:
        del ts_list[:keep_from]
    if len(ts_list) > _ROLLING_TS_MAX:
        # Hard cap — drop oldest excess. Guards against runaway chats.
        del ts_list[: len(ts_list) - _ROLLING_TS_MAX]


@dataclass
class PollState:
    last_update_id: int | None = None
    sent_comment_ids: list[str] = field(default_factory=list)
    telegram_event_ids: dict[str, int] = field(default_factory=dict)
    # Last inbound message timestamp (iso utc) per chat_id. Drives the
    # MO-9-10 inactivity-reset check: when a new inbound arrives and the
    # gap exceeds `channels.yaml::inactivity_reset_minutes`, the bridge
    # rebuilds its ChatSession so the chat history starts fresh. Keys
    # are stringified chat_ids (JSON object keys must be strings).
    last_message_ts: dict[str, str] = field(default_factory=dict)
    # Total inbound + outbound messages observed per chat_id, lifetime.
    # Feeds per-user `messages_total` and the Users pane; for the
    # ``messages_in_24h`` / ``messages_out_24h`` columns of
    # :class:`ChannelStatus` use ``recent_inbound_ts`` /
    # ``recent_outbound_ts`` below (audit fix m1 — the previous
    # implementation summed lifetime totals and called them 24h).
    messages_in_total: dict[str, int] = field(default_factory=dict)
    messages_out_total: dict[str, int] = field(default_factory=dict)
    # Rolling timestamp lists for the *real* 24h totals (audit fix m1).
    # Each list is a chronological ring of ISO timestamps. Entries
    # older than 24h are pruned on read/write so the count is always
    # accurate without scanning JSONL conversations.
    recent_inbound_ts: list[str] = field(default_factory=list)
    recent_outbound_ts: list[str] = field(default_factory=list)
    # First-seen iso utc per chat_id — populated on the first inbound
    # message after an approve(); used for `ChannelUser.first_seen`.
    first_seen: dict[str, str] = field(default_factory=dict)
    # Per-chat tier + ttl + display name written by approve(). Drives
    # `ChannelUser` projections without re-reading the allowlist file.
    # ``user_tier`` enforcement on inbound landed in audit fix M3; TTL
    # is auto-revoked when expired (bridge moves the chat back to
    # pending and replies once with an explanation).
    user_tier: dict[str, str] = field(default_factory=dict)
    user_ttl: dict[str, str] = field(default_factory=dict)
    user_display: dict[str, str] = field(default_factory=dict)
    # Per-chat offline inbox (audit fix M1). Inbound messages received
    # while ``status.override == "offline"`` land here instead of
    # silently triggering an archive-only "queued" reply. The bridge
    # drains the inbox into real turns when the override flips back to
    # online (or on boot if the override is already online and the
    # inbox is non-empty).
    offline_inbox: dict[str, list[OfflineMessage]] = field(default_factory=dict)
    # `/clear` confirmation state (2026-05-16). Keys: stringified
    # chat_id. Value: ISO timestamp of when the operator issued
    # `/clear`. The bridge's next-message handler intercepts the
    # reply (yes/no/anything-else) and either reflects+clears, just
    # clears, or cancels and falls through to normal processing.
    # Auto-expires after `CLEAR_PENDING_TTL_S` (300s).
    pending_clear: dict[str, str] = field(default_factory=dict)
    # Session 3 (2026-05-16) — per-chat "reply with voice" toggle. Keys:
    # stringified chat_id. Value: bool. When True the bridge synthesises
    # the assistant's text reply via the local TTS engine and ships it as a voice
    # note instead of plain text. Operator-controlled via /voice_on
    # /voice_off slash commands; persisted across restarts.
    reply_voice: dict[str, bool] = field(default_factory=dict)

    def remember_sent(self, comment_id: str) -> None:
        if comment_id in self.sent_comment_ids:
            return
        self.sent_comment_ids.append(comment_id)
        if len(self.sent_comment_ids) > _SENT_RING_MAX:
            del self.sent_comment_ids[: len(self.sent_comment_ids) - _SENT_RING_MAX]

    # -- rolling 24h counters (audit fix m1) ------------------------------

    def record_inbound(self, chat_key: str, ts_iso: str) -> None:
        del chat_key
        self.recent_inbound_ts.append(ts_iso)
        _prune_24h(self.recent_inbound_ts)

    def record_outbound(self, chat_key: str, ts_iso: str) -> None:
        del chat_key
        self.recent_outbound_ts.append(ts_iso)
        _prune_24h(self.recent_outbound_ts)

    def count_inbound_24h(self) -> int:
        _prune_24h(self.recent_inbound_ts)
        return len(self.recent_inbound_ts)

    def count_outbound_24h(self) -> int:
        _prune_24h(self.recent_outbound_ts)
        return len(self.recent_outbound_ts)

    # -- offline inbox (audit fix M1) -------------------------------------

    def enqueue_offline(self, chat_key: str, msg: OfflineMessage) -> int:
        """Append ``msg`` to the chat's offline inbox; return resulting depth."""
        bucket = self.offline_inbox.setdefault(chat_key, [])
        bucket.append(msg)
        if len(bucket) > _OFFLINE_INBOX_PER_CHAT_MAX:
            overflow = len(bucket) - _OFFLINE_INBOX_PER_CHAT_MAX
            del bucket[:overflow]
        return len(bucket)

    def drain_offline(self, chat_key: str) -> list[OfflineMessage]:
        """Pop and return the chat's full offline inbox (oldest-first)."""
        return self.offline_inbox.pop(chat_key, [])

    def offline_inbox_depth(self, chat_key: str | None = None) -> int:
        if chat_key is None:
            return sum(len(v) for v in self.offline_inbox.values())
        return len(self.offline_inbox.get(chat_key, []))


@dataclass
class Status:
    override: StatusOverride | None = None


def load_allowlist(path: Path, *, env_seed: str | None = None) -> Allowlist:
    raw = _read_json(path) or {}
    chat_ids: set[int] = set()
    for value in raw.get("chat_ids") or []:
        try:
            chat_ids.add(int(value))
        except (TypeError, ValueError):
            continue
    if env_seed:
        for value in env_seed.split(","):
            value = value.strip()
            if not value:
                continue
            try:
                chat_ids.add(int(value))
            except ValueError:
                continue
    pending: dict[int, PendingChat] = {}
    for row in raw.get("pending") or []:
        if not isinstance(row, dict):
            continue
        try:
            chat_id = int(row["chat_id"])
        except (KeyError, TypeError, ValueError):
            continue
        pending[chat_id] = PendingChat(
            chat_id=chat_id,
            username=row.get("username") if isinstance(row.get("username"), str) else None,
            first_seen=str(row.get("first_seen") or ""),
        )
    blocked: set[int] = set()
    for value in raw.get("blocked") or []:
        try:
            blocked.add(int(value))
        except (TypeError, ValueError):
            continue
    # A chat appearing in both `chat_ids` and `blocked` is operator error;
    # `blocked` wins (latest decision) so an unblock-then-block sequence
    # stays effective after a restart.
    chat_ids -= blocked
    return Allowlist(chat_ids=chat_ids, pending=pending, blocked=blocked)


def save_allowlist(path: Path, allowlist: Allowlist) -> None:
    _write_json(
        path,
        {
            "chat_ids": sorted(allowlist.chat_ids),
            "pending": [row.to_dict() for row in allowlist.pending.values()],
            "blocked": sorted(allowlist.blocked),
        },
    )


def load_state(path: Path) -> PollState:
    raw = _read_json(path) or {}
    state = PollState(
        last_update_id=raw.get("last_update_id")
        if isinstance(raw.get("last_update_id"), int)
        else None,
        sent_comment_ids=[
            str(x) for x in (raw.get("sent_comment_ids") or []) if isinstance(x, str)
        ],
    )
    mappings = raw.get("telegram_event_ids") or {}
    if isinstance(mappings, dict):
        for key, value in mappings.items():
            try:
                state.telegram_event_ids[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
    _load_str_dict(raw.get("last_message_ts"), state.last_message_ts)
    _load_int_dict(raw.get("messages_in_total"), state.messages_in_total)
    _load_int_dict(raw.get("messages_out_total"), state.messages_out_total)
    _load_str_dict(raw.get("first_seen"), state.first_seen)
    _load_str_dict(raw.get("user_tier"), state.user_tier)
    _load_str_dict(raw.get("user_ttl"), state.user_ttl)
    _load_str_dict(raw.get("user_display"), state.user_display)
    for entry in raw.get("recent_inbound_ts") or []:
        if isinstance(entry, str):
            state.recent_inbound_ts.append(entry)
    for entry in raw.get("recent_outbound_ts") or []:
        if isinstance(entry, str):
            state.recent_outbound_ts.append(entry)
    _prune_24h(state.recent_inbound_ts)
    _prune_24h(state.recent_outbound_ts)
    _load_str_dict(raw.get("pending_clear"), state.pending_clear)
    reply_voice_raw = raw.get("reply_voice")
    if isinstance(reply_voice_raw, dict):
        for key, value in reply_voice_raw.items():
            state.reply_voice[str(key)] = bool(value)
    offline_raw = raw.get("offline_inbox")
    if isinstance(offline_raw, dict):
        for chat_key, rows in offline_raw.items():
            if not isinstance(rows, list):
                continue
            bucket = state.offline_inbox.setdefault(str(chat_key), [])
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    bucket.append(
                        OfflineMessage(
                            ts=str(row.get("ts") or _now_iso()),
                            telegram_message_id=int(row.get("telegram_message_id") or 0),
                            text=str(row.get("text") or ""),
                            from_user_id=(
                                int(row["from_user_id"])
                                if isinstance(row.get("from_user_id"), int)
                                else None
                            ),
                            from_username=(
                                str(row.get("from_username"))
                                if isinstance(row.get("from_username"), str)
                                else None
                            ),
                            attachments=[
                                dict(a) for a in (row.get("attachments") or [])
                                if isinstance(a, dict)
                            ],
                        )
                    )
                except (TypeError, ValueError):
                    continue
    return state


def _load_str_dict(raw: object, target: dict[str, str]) -> None:
    if not isinstance(raw, dict):
        return
    for key, value in raw.items():
        if isinstance(value, str):
            target[str(key)] = value


def _load_int_dict(raw: object, target: dict[str, int]) -> None:
    if not isinstance(raw, dict):
        return
    for key, value in raw.items():
        try:
            target[str(key)] = int(value)
        except (TypeError, ValueError):
            continue


def save_state(path: Path, state: PollState) -> None:
    _prune_24h(state.recent_inbound_ts)
    _prune_24h(state.recent_outbound_ts)
    _write_json(
        path,
        {
            "last_update_id": state.last_update_id,
            "sent_comment_ids": state.sent_comment_ids[-_SENT_RING_MAX:],
            "telegram_event_ids": state.telegram_event_ids,
            "last_message_ts": state.last_message_ts,
            "messages_in_total": state.messages_in_total,
            "messages_out_total": state.messages_out_total,
            "first_seen": state.first_seen,
            "user_tier": state.user_tier,
            "user_ttl": state.user_ttl,
            "user_display": state.user_display,
            "recent_inbound_ts": state.recent_inbound_ts,
            "recent_outbound_ts": state.recent_outbound_ts,
            "pending_clear": dict(state.pending_clear),
            "reply_voice": {k: bool(v) for k, v in state.reply_voice.items()},
            "offline_inbox": {
                chat_key: [m.to_dict() for m in msgs]
                for chat_key, msgs in state.offline_inbox.items()
                if msgs
            },
        },
    )


def load_status(path: Path) -> Status:
    raw = _read_json(path) or {}
    override = raw.get("override")
    return Status(override=override if override in ("online", "offline") else None)


def save_status(path: Path, status: Status) -> None:
    _write_json(path, {"override": status.override})


class StateBundle:
    def __init__(self, dir_path: Path | None = None, *, env_seed: str | None = None) -> None:
        self.dir_path = dir_path or telegram_state_dir()
        self.allowlist_path = self.dir_path / "allowlist.json"
        self.state_path = self.dir_path / "state.json"
        self.status_path = self.dir_path / "status.json"
        self._lock = threading.RLock()
        self.allowlist = load_allowlist(self.allowlist_path, env_seed=env_seed)
        self.poll_state = load_state(self.state_path)
        self.status = load_status(self.status_path)
        if env_seed:
            save_allowlist(self.allowlist_path, self.allowlist)

    def with_lock(self) -> threading.RLock:
        return self._lock
