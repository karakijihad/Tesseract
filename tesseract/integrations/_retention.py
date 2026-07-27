"""Channel retention policy — sliding window + inactivity reset.

External channels are transient brainstorm surfaces:

- ``max_turns_in_context`` caps the chat-session history at *N* turns,
  regardless of compaction. One turn = one user message + one assistant
  reply; the trim keeps the most recent ``N * 2`` history entries plus
  the leading system message (if present at index 0).
- ``inactivity_reset_minutes`` clears the per-chat ``ChatSession`` after
  this many minutes of silence so the next message starts fresh. Memory
  and vault remain durable; only the in-context chat history resets.

Resolution: per-channel override (in the channel block) → global
``defaults.retention`` → built-in fallback. Powered by the typed
:class:`ChannelsConfig` reader; the legacy dict-based loader (and the
``channels:`` legacy-compat YAML block) was removed in the 2026-05-18
two-tier refactor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_MAX_TURNS_IN_CONTEXT = 20
DEFAULT_INACTIVITY_RESET_MINUTES = 360  # 6h


@dataclass(frozen=True)
class RetentionPolicy:
    max_turns_in_context: int
    inactivity_reset_minutes: int

    @classmethod
    def fallback(cls) -> "RetentionPolicy":
        return cls(
            max_turns_in_context=DEFAULT_MAX_TURNS_IN_CONTEXT,
            inactivity_reset_minutes=DEFAULT_INACTIVITY_RESET_MINUTES,
        )


def policy_for_channel(name: str, config: Any | None = None) -> RetentionPolicy:
    """Resolve the retention policy for ``name``.

    ``config`` accepts a :class:`ChannelsConfig` instance (preferred);
    if omitted, the canonical ``channels.yaml`` is loaded. An unknown
    channel name yields the global defaults; a missing file yields the
    built-in fallback."""
    from tesseract.integrations._channels_config import (
        ChannelsConfig,
        load_channels_config,
    )

    cfg: ChannelsConfig
    if isinstance(config, ChannelsConfig):
        cfg = config
    else:
        try:
            cfg = load_channels_config()
        except Exception:
            log.exception("policy_for_channel: load_channels_config failed; using fallback")
            return RetentionPolicy.fallback()

    resolved = cfg.resolved(name) or cfg.defaults_only(name)
    return RetentionPolicy(
        max_turns_in_context=resolved.retention.max_turns_in_context,
        inactivity_reset_minutes=resolved.retention.inactivity_reset_minutes,
    )


def should_reset_for_inactivity(
    last_message_iso: str | None,
    policy: RetentionPolicy,
    *,
    now: datetime | None = None,
) -> bool:
    """``True`` iff the gap since ``last_message_iso`` exceeds the policy.

    ``None`` / unparseable timestamps return ``False`` — a fresh chat
    has no prior message to compare against. ``now`` is injectable for
    tests; defaults to ``datetime.now(timezone.utc)``.
    """
    if not last_message_iso:
        return False
    try:
        last = datetime.fromisoformat(last_message_iso)
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return (current - last) >= timedelta(minutes=policy.inactivity_reset_minutes)


def apply_retention(history: list[dict[str, Any]], policy: RetentionPolicy) -> list[dict[str, Any]]:
    """Trim ``history`` to the sliding window — newest ``max_turns_in_context * 2``
    entries, preserving an opening system message if one sits at index 0.

    Returns the trimmed list (a new list — never mutates the input).
    """
    if not history:
        return []
    cap = max(0, int(policy.max_turns_in_context)) * 2
    if cap <= 0:
        return list(history)
    head_system: list[dict[str, Any]] = []
    body = list(history)
    if body and isinstance(body[0], dict) and body[0].get("role") == "system":
        head_system = [body[0]]
        body = body[1:]
    if len(body) <= cap:
        return head_system + body
    return head_system + body[-cap:]
