"""Workspace comment / operator-post auto-reply via controller session.

When an operator posts a comment on a workspace event, or creates a new
operator_post thread, this module dispatches a fresh TARS controller
session (the same ``dispatch_to_controller`` primitive every autonomy
surface uses — survives a backend restart, no Mirror-session dependency,
no daemon cold-fork from a web request).

The controller calls the ``workspace_reply`` tool directly, which writes
the ``WorkspaceComment(author="tars")`` to the shared ``EventStore``
(cross-process file-locked). After dispatch completes the backend reads
the newly-written tars comment(s) on the thread (via ``list_comments`` +
timestamp gate) and broadcasts ``workspace_comment_appended`` so the
open thread renders the reply live.

Hard contract:
- Controller is the ONLY writer of the reply comment. Backend does NOT
  write the reply (no double-write). ``broadcast_comment_appended`` is
  called on the comment the controller already wrote.
- ``spawn_if_missing=False`` — never cold-fork a daemon from a HTTP-
  handler-triggered background task. If the daemon is down, log + skip.
- Works with NO Mirror session attached (session-independent).
- Never raises — caller fires this fire-and-forget via ``_spawn_tracked``;
  the operator's comment/post is already durable on disk regardless.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tesseract.orchestrator.tars_controller.dispatcher import (
    DispatcherError,
    dispatch_to_controller,
)
from tesseract.paths import TESSERACT_DIR, home_logs_root
from tesseract.workspace_events.broadcast import broadcast_comment_appended
from tesseract.workspace_events.events import EventStore, WorkspaceComment, WorkspaceEvent

log = logging.getLogger(__name__)

_DEFAULT_IDLE_TIMEOUT_SECONDS = 180.0


@dataclass(frozen=True)
class WorkspaceReplyConfig:
    """Resolved from ``agenda.yaml::workspace_reply``. Frozen so a watcher
    reload swaps the whole dataclass rather than mutating under a request."""

    enabled: bool = True
    idle_timeout_seconds: float = _DEFAULT_IDLE_TIMEOUT_SECONDS

    @classmethod
    def from_yaml_block(cls, block: dict[str, Any] | None) -> "WorkspaceReplyConfig":
        if not block:
            return cls()
        enabled = block.get("enabled", True)
        if not isinstance(enabled, bool):
            log.warning(
                "workspace workspace_reply: enabled must be bool, got %r — using True",
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
                "workspace workspace_reply: invalid idle_timeout_seconds %r (%s); "
                "using %.0f",
                raw_timeout, exc, _DEFAULT_IDLE_TIMEOUT_SECONDS,
            )
            idle_timeout = _DEFAULT_IDLE_TIMEOUT_SECONDS
        return cls(enabled=enabled, idle_timeout_seconds=idle_timeout)


def load_workspace_reply_config() -> WorkspaceReplyConfig:
    """Read ``agenda.yaml::workspace_reply`` from the package config tree.

    Best-effort — a missing/unreadable file yields defaults rather than
    raising."""
    path = TESSERACT_DIR / "config" / "agenda.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        log.warning("workspace_reply: could not read %s; using defaults", path)
        return WorkspaceReplyConfig()
    return WorkspaceReplyConfig.from_yaml_block(raw.get("workspace_reply"))


def build_workspace_reply_prompt(
    *,
    event: WorkspaceEvent,
    kind: str,
    comment_text: str,
    comment_id: str,
    existing_thread: list[WorkspaceComment],
) -> str:
    """Assemble the controller directive prompt.

    Instructs TARS to call ``workspace_reply`` with the exact
    ``event_id`` + ``comment_id`` rather than returning prose —
    the reply is durable only when the controller writes it via the
    tool. ``kind`` is ``"comment"`` (operator comment on existing event)
    or ``"post"`` (new operator_post thread).
    """
    if kind == "post":
        preamble = [
            "An operator has opened a new workspace thread. Your reply",
            "starts the conversation. Reply directly and concisely.",
        ]
    else:
        preamble = [
            "An operator has posted a comment on a workspace event.",
            "Reply directly and concisely to their latest comment.",
        ]
    lines = [
        *preamble,
        "You MUST call the `workspace_reply` tool with these exact arguments:",
        f"  event_id = \"{event.event_id}\"",
        f"  comment_id = \"{comment_id}\"",
        "Do NOT produce chat text — the operator reads your answer in the",
        "workspace thread, not the chat panel. One tool call only.",
        "",
        f"Event: {event.title}",
        f"Kind: {event.kind} · Status: {event.status}",
        f"Summary: {event.summary[:400]}",
        "",
    ]
    if existing_thread:
        lines.append("Thread so far (oldest first):")
        for c in existing_thread:
            who = "Operator" if c.author == "operator" else "TARS"
            lines.append(f"  [{who}] {c.body[:300]}")
        lines.append("")
    lines.append(f"Latest operator message: {comment_text}")
    return "\n".join(lines)


def _store_for_home() -> EventStore:
    """Resolve EventStore at call time — reads env var directly so that
    ``monkeypatch.setenv('TESSERACT_HOME', tmp_path)`` works in tests
    even though ``tesseract.paths.TESSERACT_HOME`` is a module-level constant
    captured at import time."""
    import os
    home = Path(os.environ.get("TESSERACT_HOME") or TESSERACT_DIR).resolve()
    return EventStore(home_logs_root())


async def dispatch_workspace_reply(
    app: Any,
    *,
    event_id: str,
    comment_id: str,
    event: WorkspaceEvent,
    kind: str,
    comment_text: str,
    config: WorkspaceReplyConfig | None = None,
) -> WorkspaceComment | None:
    """Dispatch a controller session to write the workspace reply.

    The controller calls ``workspace_reply`` (durable — writes before
    this function returns). The backend then reads the newly-written
    tars comment and broadcasts it for live frontend update. Never
    writes the reply itself (no double-write). Never raises.

    Parameters
    ----------
    app:
        aiohttp Application (for broadcast). May be None in tests.
    event_id:
        Target workspace event.
    comment_id:
        The operator comment (or event_id for operator_post roots) the
        controller's reply should reference.
    event:
        Full WorkspaceEvent for context building.
    kind:
        ``"comment"`` or ``"post"``.
    comment_text:
        Body of the operator comment / post (injected into the prompt).
    config:
        Resolved config; defaults to ``WorkspaceReplyConfig()`` if None.
    """
    cfg = config or WorkspaceReplyConfig()
    store = _store_for_home()
    existing_thread = store.list_comments(event_id)

    prompt = build_workspace_reply_prompt(
        event=event,
        kind=kind,
        comment_text=comment_text,
        comment_id=comment_id,
        existing_thread=existing_thread,
    )

    # Snapshot comment IDs that exist BEFORE dispatch so we can identify
    # controller-written comments by exclusion. ID-based detection is robust
    # against same-microsecond timestamp collisions (which occur in tests and
    # on fast hardware where dispatch_start and c.ts land in the same tick).
    try:
        before_ids: set[str] = {
            c.comment_id for c in store.list_comments(event_id)
            if c.author == "tars"
        }
    except Exception:
        log.exception("workspace reply: pre-dispatch list_comments failed for %s", event_id)
        before_ids = set()

    try:
        result = await dispatch_to_controller(
            prompt,
            origin="mirror",
            mode="chat",
            wait_for_completion=True,
            idle_timeout_seconds=cfg.idle_timeout_seconds,
            # Never cold-spawn from a web-request-triggered background
            # task. Supervisor owns daemon lifecycle. If it's down, skip.
            spawn_if_missing=False,
        )
    except DispatcherError as exc:
        log.warning("workspace reply: dispatch failed for %s: %s", event_id, exc)
        return None
    except Exception:
        log.exception("workspace reply: unexpected dispatch error for %s", event_id)
        return None

    if result.timed_out:
        log.warning("workspace reply: controller timed out for %s", event_id)
        return None
    if result.error:
        log.warning("workspace reply: controller error for %s: %s", event_id, result.error)
        return None

    # Find the comment(s) the controller just wrote for this event.
    # New tars comments are those NOT in the pre-dispatch snapshot.
    # In the normal case exactly one comment is written; if the
    # controller called the tool more than once we broadcast all of
    # them in order (sorted ascending by ts as returned by list_comments).
    try:
        all_comments = store.list_comments(event_id)
    except Exception:
        log.exception("workspace reply: list_comments failed for %s", event_id)
        return None

    new_tars = [
        c for c in all_comments
        if c.author == "tars" and c.comment_id not in before_ids
    ]

    if not new_tars:
        log.info(
            "workspace reply: controller did not write a comment for %s "
            "(no new tars comments after dispatch)",
            event_id,
        )
        return None

    last: WorkspaceComment | None = None
    for c in new_tars:
        try:
            await broadcast_comment_appended(app, c)
            last = c
        except Exception:
            log.exception("workspace reply: broadcast failed for comment %s", c.comment_id)

    return last


__all__ = [
    "WorkspaceReplyConfig",
    "build_workspace_reply_prompt",
    "dispatch_workspace_reply",
    "load_workspace_reply_config",
]
