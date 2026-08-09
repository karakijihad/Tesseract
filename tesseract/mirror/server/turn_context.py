"""Per-task turn context for parallel synthetic workspace turns (WP-2).

Replaces ``session.workspace_origin`` (a single mutable session attribute) with
task-local context variables. ``asyncio.create_task`` snapshots the current
context, so each spawned turn gets its own view; concurrent chat + synthetic
turns no longer step on each other.

Usage::

    token = current_workspace_origin.set(origin_dict)
    try:
        await _run_turn(...)
    finally:
        current_workspace_origin.reset(token)

In the serial pre-WP-2 path this behaves identically to the old session
attribute — ``set()`` writes the value in the running task's context,
descendants see it, peers do not, and the ``reset()`` in finally restores
the prior state.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TurnState:
    """Per-turn mutable state, owned by a single ``_run_turn`` invocation.

    Codex-fix M1 (2026-05-23): the following fields used to live on
    ``ServerSession`` (one shared instance) and got mutated during the
    ``_handle_stream_chunk`` callback path. Under WP-2 concurrent
    synthetic turns those mutations raced — one turn's finalizer could
    consume the OTHER turn's reply-success flag or clear a still-pending
    tool-name-by-call mapping mid-flight. Moving them into a per-turn
    object keyed via :data:`current_turn_state` ContextVar makes each
    turn fully owns its mutable state.

    ``ServerSession``'s same-named attributes remain in place as
    transitional fallbacks for any code path the migration may have
    missed — they default to safe values (False/0/empty) and stay
    untouched by the new path. Future cleanup retires them once the
    full call graph is verified clean.
    """

    workspace_reply_succeeded: bool = False
    tool_names_by_call: dict[str, str] = field(default_factory=dict)
    turn_tool_count: int = 0
    deep_focus_latched: bool = False
    pending_happy_saves: set[str] = field(default_factory=set)
    # mirror-multi-chat P2 inc.C2: structured-tag stream-parser carry state.
    # Used to live on ``ServerSession`` (one shared instance) and was safe only
    # while ``turn_stream_lock`` serialized ALL chat turns. inc.C2 drops that
    # lock for background turns so non-active chats stream text in parallel —
    # which means every turn now runs ``_split_text_for_surfaces`` concurrently.
    # Per-turn ownership keeps each turn's partial ``<intent>``/``<answer>`` tag,
    # tag state, and one-shot untagged warning from clobbering another's.
    stream_status_buffer: str = ""
    stream_tag_state: str = "outside"  # "outside" | "intent" | "spoken" | "answer"
    stream_untagged_warned: bool = False
    # per-turn TTS state, migrated off ServerSession (the
    # deferred "Per-chat TTS state" item). One turn per chat, so per-turn
    # ownership == per-chat ownership: two concurrently-streaming chats each
    # accumulate/sequence/chain their own audio and a mid-switch flush can't
    # clobber the other turn's buffer. Out-of-turn cancel paths (chat switch,
    # Stop button, WS cleanup) reach these via
    # ``ServerSession.turn_states_by_chat``.
    tts_buffer: str = ""
    tts_buffer_kind: str = "answer"
    # Latched by the first `<spoken>` delta of the turn; mutes every later
    # `<answer>` delta from TTS while it still streams to screen.
    tts_spoken_seen: bool = False
    tts_sequence: int = 0
    tts_synth_task: asyncio.Task | None = None
    tts_failure_notified: bool = False


# Per-task ContextVar holding the active turn's mutable state.
current_turn_state: ContextVar[TurnState | None] = ContextVar(
    "tesseract.current_turn_state",
    default=None,
)


def get_turn_state() -> TurnState | None:
    """Return the TurnState for the running task, or None if no turn is active."""
    return current_turn_state.get()

# The workspace_origin dict for the current turn, or None for a chat turn.
# Synthetic workspace turns (operator comment / operator_post) set this at
# the top of the spawned task body; chat turns leave it null.
current_workspace_origin: ContextVar[dict[str, Any] | None] = ContextVar(
    "tesseract.current_workspace_origin",
    default=None,
)

# Stable per-turn identifier. Populated by `_run_turn` so envelopes emitted
# during the turn carry a discriminator the frontend can route on.
current_turn_id: ContextVar[str | None] = ContextVar(
    "tesseract.current_turn_id",
    default=None,
)

# mirror-multi-chat P2 — the chat_id the running turn belongs to. Populated by
# `_run_turn` so turn-scoped envelopes auto-carry the chat they belong to; the
# frontend routes them to that chat's slice. None outside a turn, so
# session-scoped events (voice_*, session_*, broadcasts) stay unrouted.
current_chat_id: ContextVar[str | None] = ContextVar(
    "tesseract.current_chat_id",
    default=None,
)

def get_workspace_origin() -> dict[str, Any] | None:
    """Return the workspace_origin dict for the running task, or None."""
    return current_workspace_origin.get()


def get_turn_id() -> str | None:
    """Return the turn_id for the running task, or None when no turn is active."""
    return current_turn_id.get()


def get_chat_id() -> str | None:
    """Return the chat_id for the running turn, or None when no turn is active."""
    return current_chat_id.get()


def tts_suppressed(session: Any) -> bool:
    """True when the running turn must stay silent (D8: voice speaks to the
    active chat only).

    mirror-multi-chat P2 inc.C2 — DYNAMIC: evaluated live against
    ``session.active_chat_id`` at each emit, not latched at turn start. So
    switching the active chat away from a still-streaming turn silences it
    immediately, and switching back un-silences it — the voice follows the
    operator's active chat. A turn whose chat IS the active chat speaks; a turn
    with no chat_id (synthetic / legacy callers) is audible.
    """
    cid = current_chat_id.get()
    return cid is not None and cid != getattr(session, "active_chat_id", cid)
