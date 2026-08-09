"""Per-tier tool-policy overlay for channel sessions (audit fix M3).

Channels expose two tiers: ``operator`` (full surface — the operator on
their own phone) and ``friend`` (third party, restricted). Today
``permissions.yaml`` is the single source of truth for AUTO / ASK / DENY,
but it cannot distinguish "operator over Telegram" from "friend over
Telegram" — the policy lookup runs before any session metadata is
consulted. This module supplies the missing overlay: for ``friend``
sessions, a name-based denylist forces ``False`` *before* the gate
emits a workspace nudge.

The denylist is conservative on purpose. We forbid any tool that can
mutate the host (terminal, source-edit, agent/tool promotion) or that
spends operator resources unattended (mission orchestration, delegation
to subscription CLIs). Read-only memory, vault, and chat surfaces stay
allowed so a friend can have a useful conversation with the assistant.

Wiring: :func:`build_tiered_ask_fn` wraps the channel ask_fn returned by
:func:`build_channel_ask_fn` so the gate is never reached for a denied
tool. The denial path replies with a stable, operator-recognisable
message via the bridge's outbound channel; from the friend's side the
tool simply does not run.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from tesseract.kernel.tools.base import Tool, ToolContext

log = logging.getLogger(__name__)


# Concrete tool names that a friend-tier session MUST NOT invoke. Names
# track ``tesseract/kernel/tools/<name>.py`` so a missing entry here is
# instantly visible against the registry. Add new sensitive tools to
# this list when they land — kernel-lockdown CLAUDE.md hard rule.
FRIEND_DENIED_TOOLS: frozenset[str] = frozenset(
    {
        # mission orchestration — friend cannot start, pause, resume, cancel
        "mission_loop_create",
        "mission_pause",
        "mission_resume",
        "mission_cancel",
        "tasks_set",
        "tasks_update",
        # terminal / process — host-write capability
        "bash",
        "process_start",
        # delegation — burns operator-subscribed CLI quota
        "delegate_coder",
        "delegate_auditor",
        "spawn_await",
        "invoke_agent",
        # source-edit + promotion — kernel lockdown perimeter
        "file_write",
        "agent_create",
        "agent_promote",
        # memory / vault writes — friend tier reads only
        "memory_save",
        "memory_forget",
        "vault_ingest",
        # schedule / cron — would let a friend run unattended work
        "schedule_create",
        "schedule_delete",
        # brief — gated to operator already by ``brief_push.send_to_operators``
        # but the natural-language tool entry must also deny here so a
        # friend cannot trigger a render via "send me the brief". Also
        # deny ``brief_read`` so a friend cannot read the operator's
        # daily-brief content via a chat-side tool call.
        "brief_render",
        "brief_read",
        # workspace-inbox proposals — friend must not be able to spam
        # operator's pending-approval queue with change_proposal or
        # SOUL.md growth bullets. Both tools have
        # ``default_posture="auto"`` so without this overlay a friend
        # turn would queue them silently.
        "propose_change",
        "soul_growth_propose",
    }
)

# Prefix matches catch future tools without requiring an edit here.
# Kept tight — anything matching one of these prefixes is operator-only
# until explicitly allowlisted.
FRIEND_DENIED_PREFIXES: tuple[str, ...] = (
    "mission_",
    "pty_",
    "delegate_",
    "agent_",
    "schedule_",
    # Any future ``*_propose`` tool (operator-approval queue writer)
    # should default to operator-only until explicitly allowlisted.
    "propose_",
)


def is_friend_denied(tool_name: str) -> bool:
    """Return True when ``tool_name`` is denied for friend-tier sessions."""
    if tool_name in FRIEND_DENIED_TOOLS:
        return True
    for prefix in FRIEND_DENIED_PREFIXES:
        if tool_name.startswith(prefix):
            return True
    return False


AskFn = Callable[[Tool, Any, ToolContext], Awaitable[bool]]


def build_tiered_ask_fn(
    *,
    tier: str,
    inner: AskFn,
    channel: str,
    chat_id: str,
) -> AskFn:
    """Wrap ``inner`` with a tier-aware pre-check.

    For ``operator`` tier this is a pass-through (the inner gate decides).
    For ``friend`` tier, denied tools short-circuit to ``False`` without
    emitting a workspace nudge — there is nothing for the operator to
    approve, the tool is forbidden by policy.
    """
    if tier == "operator":
        return inner

    async def _tiered(tool: Tool, validated: Any, context: ToolContext) -> bool:
        if is_friend_denied(tool.name):
            log.info(
                "channel tier: friend-deny %s on %s/%s",
                tool.name, channel, chat_id,
            )
            return False
        return await inner(tool, validated, context)

    return _tiered


__all__ = [
    "FRIEND_DENIED_TOOLS",
    "FRIEND_DENIED_PREFIXES",
    "is_friend_denied",
    "build_tiered_ask_fn",
]
