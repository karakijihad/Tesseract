"""Agenda comment auto-reply — operator comment -> TARS answers in-thread.

Option-B durability (matches ``workspace_reply_dispatch.py``): the
controller calls the ``agenda_comment`` tool directly, which writes the
``role="agent"`` comment to the shared per-item JSONL log. This module
dispatches a fresh controller session (the same ``dispatch_to_controller``
primitive every autonomy surface uses — survives a backend restart, no
Mirror-session dependency, no daemon cold-fork from a web request), then
reads the newly-written agent comment(s) and broadcasts
``agenda_comment_added`` so the open detail modal renders the reply live.

Hard contract:
- Controller is the ONLY writer of the reply comment (via the
  ``agenda_comment`` tool). This module does NOT write the reply itself
  (no double-write, no backend-crash-loses-reply gap).
- ``spawn_if_missing=False`` — never cold-fork a daemon from a HTTP-
  handler-triggered background task. If the daemon is down, log + skip.
- Works with NO Mirror session attached (session-independent).
- Never raises — caller fires this fire-and-forget via ``_spawn_tracked``;
  the operator's comment is already durable on disk regardless.

Parallelism (operator directive 2026-05-24): replies are NOT serialized
per thread — each comment mints its own controller session, so multiple
operators / items answer concurrently.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import yaml

from tesseract.orchestrator.autonomy.agenda_comments import (
    AgendaComment,
    list_comments,
)
from tesseract.orchestrator.autonomy.broadcast import (
    broadcast_agenda_comment_event,
)
from tesseract.orchestrator.autonomy.models import AgendaItem
from tesseract.orchestrator.tars_controller.dispatcher import (
    DispatcherError,
    dispatch_to_controller,
)
from tesseract.paths import TESSERACT_DIR

log = logging.getLogger(__name__)

# Fallback used only when agenda.yaml omits the key — the authoritative
# value lives in agenda.yaml::comment_reply (operator-tunable). Mirrors
# the FollowUpConfig pattern.
_DEFAULT_IDLE_TIMEOUT_SECONDS = 180.0


@dataclass(frozen=True)
class AgendaReplyConfig:
    """Resolved from ``agenda.yaml::comment_reply``. Frozen so a watcher
    reload swaps the whole dataclass rather than mutating under a request."""

    enabled: bool = True
    idle_timeout_seconds: float = _DEFAULT_IDLE_TIMEOUT_SECONDS

    @classmethod
    def from_yaml_block(cls, block: dict[str, Any] | None) -> "AgendaReplyConfig":
        """Tolerant loader — a missing block or bad value falls back to the
        default rather than raising at load time."""
        if not block:
            return cls()
        enabled = block.get("enabled", True)
        if not isinstance(enabled, bool):
            log.warning(
                "agenda comment_reply: enabled must be bool, got %r — using True",
                enabled,
            )
            enabled = True
        raw_timeout = block.get("idle_timeout_seconds", _DEFAULT_IDLE_TIMEOUT_SECONDS)
        try:
            idle_timeout = float(raw_timeout)
            if idle_timeout <= 0:
                raise ValueError("must be positive")
        except (TypeError, ValueError) as exc:
            log.warning(
                "agenda comment_reply: invalid idle_timeout_seconds %r (%s); "
                "using %.0f",
                raw_timeout, exc, _DEFAULT_IDLE_TIMEOUT_SECONDS,
            )
            idle_timeout = _DEFAULT_IDLE_TIMEOUT_SECONDS
        return cls(enabled=enabled, idle_timeout_seconds=idle_timeout)


def load_agenda_reply_config() -> AgendaReplyConfig:
    """Read ``agenda.yaml::comment_reply`` from the source config tree.

    Config lives under ``TESSERACT_DIR`` (the package), not user-state, so
    it follows the install. Best-effort — a missing / unreadable file
    yields defaults rather than raising."""
    path = TESSERACT_DIR / "config" / "agenda.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        log.warning("agenda comment_reply: could not read %s; using defaults", path)
        return AgendaReplyConfig()
    return AgendaReplyConfig.from_yaml_block(raw.get("comment_reply"))


def _agenda_comment_payload(comment: AgendaComment) -> dict[str, Any]:
    """Broadcast payload shape — mirrors ``agenda.py::_comment_payload``."""
    return {
        "id": comment.id,
        "at": comment.at.isoformat(),
        "role": comment.role,
        "by": comment.by,
        "body": comment.body,
    }


def build_agenda_reply_prompt(
    item: AgendaItem, thread: list[AgendaComment]
) -> str:
    """Assemble the controller directive prompt.

    Instructs TARS to call the ``agenda_comment`` tool with the exact
    ``item_id`` rather than returning prose — the reply is durable only
    when the controller writes it via the tool."""
    gates = ", ".join(
        f"{g.kind}:{g.target}" + ("✓" if g.fulfilled else "")
        for g in item.approvals_required
    ) or "none"
    lines = [
        "An operator has posted a comment on an agenda item in its comment",
        "thread. Reply directly and concisely to their latest comment — they",
        "are deciding whether to approve, snooze, or cancel this item.",
        "You MUST call the `agenda_comment` tool with these exact arguments:",
        f"  item_id = \"{item.id}\"",
        "Do NOT produce chat text — the operator reads your answer in the",
        "agenda comment thread, not the chat panel. One tool call only.",
        "",
        f"Item: {item.goal}",
        f"Status: {item.status.value} · risk: {item.risk_class.value} · "
        f"score: {item.priority_score:.2f}",
        f"Approval gates: {gates}",
    ]
    if item.rationale:
        lines.append(f"Rationale: {item.rationale}")
    lines.append("")
    lines.append("Comment thread (oldest first):")
    for c in thread:
        who = "Operator" if c.role == "operator" else "TARS"
        lines.append(f"  [{who}] {c.body}")
    return "\n".join(lines)


async def dispatch_agenda_reply(
    app: Any,
    *,
    item: AgendaItem,
    thread: list[AgendaComment],
    config: AgendaReplyConfig | None = None,
) -> AgendaComment | None:
    """Dispatch a controller session to write the agenda reply.

    The controller calls ``agenda_comment`` (durable — writes before
    this function returns). The backend then reads the newly-written
    agent comment and broadcasts it for live frontend update. Never
    writes the reply itself (no double-write). Never raises.

    Parameters
    ----------
    app:
        aiohttp Application (for broadcast). May be None in tests.
    item:
        Full AgendaItem for prompt context.
    thread:
        The comment thread as of just before dispatch (used both for
        prompt context and as the pre-dispatch snapshot for detecting
        the controller's new comment by exclusion).
    config:
        Resolved config; defaults to ``AgendaReplyConfig()`` if None.
    """
    cfg = config or AgendaReplyConfig()
    prompt = build_agenda_reply_prompt(item, thread)

    # Snapshot agent-comment ids that exist BEFORE dispatch so we can
    # identify the controller-written comment by exclusion. ID-based
    # detection is robust against same-microsecond timestamp collisions.
    before_ids: set[str] = {c.id for c in thread if c.role == "agent"}

    try:
        result = await dispatch_to_controller(
            prompt,
            origin="mirror",
            mode="chat",
            wait_for_completion=True,
            idle_timeout_seconds=cfg.idle_timeout_seconds,
            # Never cold-spawn a controller daemon from a web-request-
            # triggered background task — the supervisor owns the daemon
            # lifecycle (daemon.py::_spawn_controller_daemon + watchdog).
            # If it's somehow down, log + skip rather than fork a
            # subprocess out of an HTTP handler. Also keeps tests that POST
            # comments from spawning real daemons.
            spawn_if_missing=False,
        )
    except DispatcherError as exc:
        log.warning("agenda reply: dispatch failed for %s: %s", item.id, exc)
        return None
    except Exception:
        log.exception("agenda reply: unexpected dispatch error for %s", item.id)
        return None

    if result.timed_out:
        log.warning("agenda reply: controller timed out for %s", item.id)
        return None
    if result.error:
        log.warning("agenda reply: controller error for %s: %s", item.id, result.error)
        return None

    # Find the comment(s) the controller just wrote for this item.
    # New agent comments are those NOT in the pre-dispatch snapshot. In
    # the normal case exactly one comment is written; if the controller
    # called the tool more than once we broadcast all of them in order
    # (as returned by list_comments, chronological).
    try:
        all_comments = list_comments(item.id)
    except Exception:
        log.exception("agenda reply: list_comments failed for %s", item.id)
        return None

    new_agent = [
        c for c in all_comments
        if c.role == "agent" and c.id not in before_ids
    ]

    if not new_agent:
        log.info(
            "agenda reply: controller did not write a comment for %s "
            "(no new agent comments after dispatch)",
            item.id,
        )
        return None

    last: AgendaComment | None = None
    for c in new_agent:
        try:
            await broadcast_agenda_comment_event(
                app,
                "agenda_comment_added",
                item_id=item.id,
                comment=_agenda_comment_payload(c),
            )
            last = c
        except Exception:
            log.exception("agenda reply: broadcast failed for comment %s", c.id)

    return last


__all__ = [
    "AgendaReplyConfig",
    "build_agenda_reply_prompt",
    "dispatch_agenda_reply",
    "load_agenda_reply_config",
]
