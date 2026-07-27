"""Channel gate-on-ASK → workspace nudge pattern (CR-5).

Channel sessions have no operator at the cockpit. When a tool would ASK,
the old behavior was ``_deny_ask`` → ``False`` → opaque "I can't do that"
reply from TARS. This module replaces that with the *workspace nudge*
pattern: emit a ``tars_post`` :class:`WorkspaceEvent` carrying the
conversation context + tool + args + reason, then return ``False`` (the
tool stays denied this turn — operator handles asynchronously).

The bridge wires :func:`build_channel_ask_fn` into the headless
``ServerSession`` via ``_build_chat_session(ask_fn=…)`` so the channel
turn loop and the cockpit turn loop share the same call site; only the
ASK resolution differs.

Two operator-side actions on the emitted event power the round-trip:

- **Approve next turn** — sets a per-session token keyed on
  ``(tool_name, args_hash)`` with a TTL from
  ``channels.yaml::<channel>.gate_policy.approve_next_turn_ttl_s``. The
  next call that hashes to the same key auto-passes, bypassing this
  module entirely (returns ``True`` without re-emitting).
- **Reject & message user** — operator picks a templated reply that the
  bridge sends as an outbound channel message; the event closes.

Per-turn dedup: a session can only emit one ``tars_post`` per unique
``(tool_name, args_hash)`` per turn. The state lives on the
``ServerSession`` via attached dicts (we don't add fields to the frozen
dataclass to keep the change pure-addition).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from tesseract.integrations._conversation_store import ConversationStore
from tesseract.kernel.tools.base import Tool, ToolContext
from tesseract.mirror.server.session import ServerSession
from tesseract.workspace_events.events import EventStore, WorkspaceEvent

log = logging.getLogger(__name__)


# Per-turn dedup attaches to ``ServerSession`` under this attribute name
# so the frozen ``WorkspaceEvent`` and ``ServerSession`` dataclasses stay
# pure-addition compatible. ``_per_turn_emitted`` is a set of
# ``args_hash`` strings reset at the top of each turn.
_PER_TURN_ATTR = "_channel_gate_per_turn_emitted"

# Single-shot approvals attach under this attribute. Mapping
# ``args_hash -> expires_at_epoch``.
_APPROVALS_ATTR = "_channel_gate_pending_approvals"

# Cap on how many recent message rows we attach to the workspace event
# payload. Operator needs enough context to decide; full transcripts
# bloat the events.jsonl. 10 = a few back-and-forth turns.
TRANSCRIPT_TAIL_DEFAULT = 10


# ---------------------------------------------------------------------- public


@dataclass(frozen=True)
class GatedCall:
    tool_name: str
    args_hash: str


def args_fingerprint(tool_name: str, args: Any) -> str:
    """Stable hash of (tool_name, normalized args) for dedup + approval keys.

    Same arguments in a different dict order must hash to the same value;
    we sort keys recursively before hashing. Non-JSON-serializable values
    are coerced to ``repr`` so a stray object never crashes the gate.
    """
    payload = {"tool": tool_name, "args": _canonicalize(args)}
    blob = json.dumps(payload, sort_keys=True, default=repr).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:16]


def _canonicalize(value: Any) -> Any:
    """Recursive sort-by-key + JSON-friendly coercion for hashing."""
    if isinstance(value, dict):
        return {k: _canonicalize(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def reset_per_turn_state(session: ServerSession) -> None:
    """Clear the per-turn dedup set. Called by the bridge at turn entry."""
    setattr(session, _PER_TURN_ATTR, set())


def _per_turn_set(session: ServerSession) -> set[str]:
    """Return the live (mutable) per-turn dedup set, lazy-allocating once.

    The set is shared by every concurrent ``_ask`` call against the same
    session — two safe-to-parallelize tools (``chat.py`` runs those via
    ``asyncio.create_task``) can both enter the gate at the same time, so
    the dedup check + ``set.add`` MUST happen on the same object. Reading
    a copy and ``setattr``-ing it back would let two concurrent calls
    pass the ``in per_turn`` guard before either persisted the hash.
    """
    existing = getattr(session, _PER_TURN_ATTR, None)
    if isinstance(existing, set):
        return existing
    fresh: set[str] = set()
    setattr(session, _PER_TURN_ATTR, fresh)
    return fresh


def record_approval(
    session: ServerSession,
    *,
    tool_name: str,
    args: dict[str, Any],
    ttl_s: int,
) -> str:
    """Stash a single-shot approval token.

    Returns the ``args_hash`` so the inbox handler can echo it back to
    the operator (or log it for debugging). The token expires after
    ``ttl_s`` seconds — operator approval that the channel user never
    triggers must not auto-pass an unrelated future call.
    """
    return record_approval_by_hash(
        session,
        args_hash=args_fingerprint(tool_name, args),
        ttl_s=ttl_s,
    )


def record_approval_by_hash(
    session: ServerSession,
    *,
    args_hash: str,
    ttl_s: int,
) -> str:
    """Stash an approval token keyed by a pre-computed fingerprint.

    The inbox route uses this directly so an operator approval is keyed
    on the *exact* hash the gate computed (which is stored in the
    workspace-event payload), avoiding any risk that re-deriving the
    hash from a re-serialized args dict produces a different fingerprint.
    """
    approvals: dict[str, float] = getattr(session, _APPROVALS_ATTR, None) or {}
    approvals[args_hash] = time.time() + max(0, ttl_s)
    setattr(session, _APPROVALS_ATTR, approvals)
    return args_hash


def consume_approval(
    session: ServerSession,
    *,
    tool_name: str,
    args: Any,
) -> bool:
    """Return True (and consume) when a matching single-shot approval is live.

    Expired tokens are dropped silently. Mismatches leave the existing
    tokens in place so a different upcoming call can still match.
    """
    approvals: dict[str, float] = getattr(session, _APPROVALS_ATTR, None) or {}
    h = args_fingerprint(tool_name, args)
    expires = approvals.get(h)
    now = time.time()
    # Sweep expired tokens opportunistically so the dict doesn't grow.
    for key in list(approvals):
        if approvals[key] <= now:
            approvals.pop(key, None)
    if expires is None or expires <= now:
        return False
    approvals.pop(h, None)
    return True


# ---------------------------------------------------------------------- ask_fn


GateEmitter = Callable[[WorkspaceEvent], Awaitable[None]]


def build_channel_ask_fn(
    *,
    session: ServerSession,
    channel: str,
    chat_id: str,
    display_name: str,
    event_store: EventStore | None,
    conversation_store: ConversationStore | None,
    approve_next_turn_ttl_s: int,
    transcript_tail_n: int = TRANSCRIPT_TAIL_DEFAULT,
    broadcast: GateEmitter | None = None,
) -> Callable[[Tool, Any, ToolContext], Awaitable[bool]]:
    """Build a channel-aware ``ask_fn`` that nudges the workspace inbox.

    The returned callable is wired into ``ChatSession.ask_fn`` via
    ``_build_chat_session`` so every ASK posture on a channel session
    funnels through it. Three exit paths:

    1. **Approval already in hand** — consume the single-shot token and
       return ``True``; tool runs normally.
    2. **Already gated this turn** — operator already saw a ``tars_post``
       for this ``(tool, args_hash)`` in the same turn; skip re-emit and
       return ``False`` so TARS doesn't spam the inbox in tight loops.
    3. **Fresh gate** — emit one ``tars_post`` carrying the conversation
       context + tool name + args + reason, return ``False``.
    """

    async def _ask(tool: Tool, validated: Any, context: ToolContext) -> bool:
        args_dict = _validated_to_dict(validated)
        if consume_approval(session, tool_name=tool.name, args=args_dict):
            log.info(
                "channel gate: approval consumed for %s on %s/%s",
                tool.name, channel, chat_id,
            )
            return True

        per_turn = _per_turn_set(session)
        h = args_fingerprint(tool.name, args_dict)
        if h in per_turn:
            log.info(
                "channel gate: dedup hit %s/%s (tool=%s hash=%s) — no re-emit",
                channel, chat_id, tool.name, h,
            )
            return False
        # Claim the slot *before* any await — two concurrent safe tools
        # in the same turn (``chat.py`` fans them out via
        # ``asyncio.create_task``) must not both enter the emit block.
        # ``set.add`` on the shared object is observable to the sibling
        # task immediately; the next iteration's ``in per_turn`` check
        # will short-circuit.
        per_turn.add(h)

        if event_store is None:
            log.warning(
                "channel gate: workspace_event_store unavailable; falling back to deny"
            )
            return False

        ask_reason = ""
        getter = getattr(tool, "ask_reason", None)
        if callable(getter):
            try:
                ask_reason = str(getter(validated) or "")
            except Exception:
                log.exception(
                    "channel gate: tool.ask_reason raised for %s — leaving blank",
                    tool.name,
                )

        transcript_tail = _recent_messages(
            conversation_store, channel, chat_id, transcript_tail_n
        )
        recent_user = _latest_user_body(transcript_tail)

        title = f"Channel turn paused — TARS wanted to call {tool.name}"
        summary = (
            f"{display_name} ({chat_id}): " + (recent_user[:140] if recent_user else "")
        ).rstrip()
        author_id = f"{channel}:{chat_id}"
        payload = {
            "channel": channel,
            "chat_id": chat_id,
            "session_id": context.session_id or session.session_id,
            "tool": tool.name,
            "args": args_dict,
            "args_hash": h,
            "reason": ask_reason,
            "transcript_tail": transcript_tail,
            "approve_next_turn_ttl_s": int(approve_next_turn_ttl_s),
            "posture_source": context.posture_source,
        }

        event = WorkspaceEvent.new(
            kind="tars_post",
            source="tars",
            title=title,
            summary=summary,
            payload=payload,
            priority=6,
            author_id=author_id,
            author_display=display_name,
        )
        try:
            event_store.append_event(event)
        except Exception:
            log.exception("channel gate: append_event failed for %s", tool.name)
            # Release the dedup slot so the operator's manual retry (or
            # a sibling call) gets a second chance to land a workspace
            # event rather than being silently swallowed.
            per_turn.discard(h)
            return False

        if broadcast is not None:
            try:
                await broadcast(event)
            except Exception:
                log.exception(
                    "channel gate: broadcast failed for event %s", event.event_id
                )

        log.info(
            "channel gate: emitted tars_post %s for %s/%s tool=%s",
            event.event_id, channel, chat_id, tool.name,
        )
        return False

    return _ask


def _validated_to_dict(validated: Any) -> dict[str, Any]:
    """Pydantic v2 model → dict; fall back to ``__dict__`` for plain objects."""
    if validated is None:
        return {}
    dumper = getattr(validated, "model_dump", None)
    if callable(dumper):
        try:
            data = dumper(mode="json")
        except Exception:
            data = dumper()
        if isinstance(data, dict):
            return data
    if isinstance(validated, dict):
        return dict(validated)
    return {"value": repr(validated)}


def _recent_messages(
    conversation_store: ConversationStore | None,
    channel: str,
    chat_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    if conversation_store is None or limit <= 0:
        return []
    try:
        rows = conversation_store.tail(channel, chat_id, limit=limit)
    except Exception:
        log.exception("channel gate: conversation tail failed")
        return []
    return list(reversed(rows))  # oldest-first for the operator


def _latest_user_body(rows: list[dict[str, Any]]) -> str:
    for row in reversed(rows):
        if row.get("direction") == "inbound":
            body = row.get("body")
            if isinstance(body, str) and body.strip():
                return body.strip()
    return ""
