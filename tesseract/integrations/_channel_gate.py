"""Channel gate-on-ASK — the cockpit's approval flow, reached from a phone.

A channel session has no operator at the cockpit, but that changes *where*
the question is asked, not how it is answered. `mirror/server/ask_gate.py`
creates a future, shows the operator the call, **awaits the decision**, and
returns it to `permissions/decide.py::evaluate`, which then runs the tool in
the same turn. This module does exactly that, with the question delivered as
a workspace event plus a Telegram inline keyboard instead of a WS envelope.
One entry point, one resolution, whichever surface the operator answers on.

It did not always. Until 2026-08-17 this gate emitted its event and returned
``False`` immediately, then tried to reconstruct consent afterwards: the
operator's tap stashed a token keyed on ``(tool_name, args_hash)``, and the
*next* call that hashed identically was let through. Approving therefore ran
nothing and scheduled nothing — the turn was already over — and the token
only ever redeemed if the model spontaneously re-issued a byte-identical
call. It normally did not, so an approval reliably did nothing at all while
the button said "the assistant will retry next turn". Awaiting the answer
deletes the token, the fingerprint match, the TTL and the whole next-turn
concept along with the bug.

Two operator-side actions resolve the pending future, from either surface:

- **Approve** — the tool runs, in the turn the operator was already looking
  at, and its result reaches the channel reply.
- **Reject** — resolves ``False``; `decide.evaluate` hands the model the
  standard not-approved result and the turn continues without the call.

**The chat lock is why a timeout exists.** The bridge serialises a chat's
turns behind ``_chat_locks[chat_id]``, so a turn parked on this future holds
that chat until it resolves. `decision_timeout_s` bounds the wait, and the
bridge additionally cancels a chat's pending asks the moment a new inbound
message arrives — a user who keeps talking has moved on, and must never find
the bot deaf because they ignored a prompt.

Per-turn suppression: once a ``(tool_name, args_hash)`` has been *refused* in
a turn, further identical calls in that same turn are refused without asking
again. This is the one place the channel path deliberately differs from the
cockpit, where the operator is present and can answer a repeat immediately;
here a model loop would otherwise park the chat once per iteration. The state
lives on the ``ServerSession`` via an attached set (we don't add fields to the
frozen dataclass to keep the change pure-addition).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from tesseract.integrations._conversation_store import ConversationStore
from tesseract.kernel.tools.base import Tool, ToolContext
from tesseract.mirror.server.session import ServerSession
from tesseract.workspace_events.events import EventStore, WorkspaceEvent

log = logging.getLogger(__name__)


# Per-turn refusals attach to ``ServerSession`` under this attribute name
# so the frozen ``WorkspaceEvent`` and ``ServerSession`` dataclasses stay
# pure-addition compatible. A set of ``args_hash`` strings, reset at the top
# of each turn, holding what the operator already said no to *this* turn.
_PER_TURN_ATTR = "_channel_gate_per_turn_refused"

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
    """Clear the per-turn refusal set. Called by the bridge at turn entry."""
    setattr(session, _PER_TURN_ATTR, set())


def _per_turn_set(session: ServerSession) -> set[str]:
    """Return the live (mutable) per-turn refusal set, lazy-allocating once.

    The set is shared by every concurrent ``_ask`` call against the same
    session — two safe-to-parallelize tools (``chat.py`` runs those via
    ``asyncio.create_task``) can both enter the gate at the same time, so
    the membership check and the ``set.add`` MUST happen on the same object.
    Reading a copy and ``setattr``-ing it back would let a refusal recorded
    by one task go unseen by its sibling.
    """
    existing = getattr(session, _PER_TURN_ATTR, None)
    if isinstance(existing, set):
        return existing
    fresh: set[str] = set()
    setattr(session, _PER_TURN_ATTR, fresh)
    return fresh


@dataclass
class PendingAsk:
    """One gated call, waiting for whichever surface answers first.

    Keyed by ``event_id`` because that is the one identifier both
    resolvers already hold: the Telegram callback carries it in its
    ``callback_data``, and the Mirror inbox acts on the event itself.
    """

    event_id: str
    chat_id: str
    tool_name: str
    args_hash: str
    future: asyncio.Future[bool] = field(repr=False)


#: ``event_id -> PendingAsk``. Owned by the bridge so both the Telegram
#: callback and the Mirror inbox route reach the same futures; a channel with
#: no bridge instance simply has no pending asks to resolve.
PendingAsks = dict[str, PendingAsk]


def resolve_channel_ask(
    pending: PendingAsks, event_id: str, *, approved: bool,
) -> PendingAsk | None:
    """Answer a gated call. Returns the entry, or ``None`` if it is gone.

    ``None`` means the wait already ended — timed out, cancelled by a new
    inbound message, or answered on the other surface a moment earlier. The
    caller tells the operator that rather than pretending the tap landed,
    which is the whole reason this returns the entry instead of a bool.
    """
    entry = pending.pop(event_id, None)
    if entry is None:
        return None
    if not entry.future.done():
        entry.future.set_result(approved)
    return entry


def cancel_chat_asks(pending: PendingAsks, chat_id: str) -> list[PendingAsk]:
    """Refuse every pending ask on ``chat_id`` and return what was cancelled.

    The bridge calls this the instant a new inbound message arrives, BEFORE
    taking the per-chat lock — a turn parked here holds that lock, so waiting
    for it first would deadlock the very message meant to break the wait. A
    user who keeps talking has moved on from the prompt; leaving the chat
    unresponsive until a 30-minute timer expired would be a worse failure
    than the one this gate exists to report.
    """
    doomed = [e for e in pending.values() if e.chat_id == chat_id]
    for entry in doomed:
        pending.pop(entry.event_id, None)
        if not entry.future.done():
            entry.future.set_result(False)
    return doomed


# ---------------------------------------------------------------------- ask_fn


#: Puts the question in front of the operator ON the channel it came from.
#: Takes the prompt id the decision will be answered against, plus what is
#: being asked. Returns True when the operator can actually see it — a False
#: or a raise means the question was never delivered and waiting for an answer
#: would be waiting for nobody.
ChannelAsker = Callable[[str, str, dict[str, Any], str], Awaitable[bool]]


def build_channel_ask_fn(
    *,
    session: ServerSession,
    channel: str,
    chat_id: str,
    display_name: str,
    event_store: EventStore | None,
    conversation_store: ConversationStore | None,
    pending_asks: PendingAsks,
    decision_timeout_s: int,
    ask_on_channel: ChannelAsker,
    transcript_tail_n: int = TRANSCRIPT_TAIL_DEFAULT,
) -> Callable[[Tool, Any, ToolContext], Awaitable[bool]]:
    """Build a channel-aware ``ask_fn`` that asks on the channel, and waits.

    Wired into ``ChatSession.ask_fn`` via ``_build_chat_session``, so every ASK
    posture on a channel session funnels through the same call site the cockpit
    uses. Four exit paths:

    1. **Already refused this turn** — the operator said no to this exact
       ``(tool, args_hash)`` a moment ago; refuse again without asking, so a
       looping model cannot park the chat once per iteration.
    2. **Undeliverable** — ``ask_on_channel`` could not put the question in
       front of them. Refuse now rather than park the chat waiting on a prompt
       that does not exist.
    3. **Answered** — approved or rejected; return it and let
       `decide.evaluate` run or skip the tool.
    4. **Timed out or superseded** — nobody answered inside
       ``decision_timeout_s``, or a new inbound message cancelled the wait.
       Refuse; the model is told it was not approved, which is true.

    **The workspace is not one of those paths.** It used to be: the question
    was a workspace inbox card, and the channel prompt was a notification
    broadcast off it. That put the decision on the one surface an operator away
    from their desk cannot reach, and delivered two prompts for one call. The
    operator's rule — *"inputs are a funnel… the moment you send the
    communication it should pass"* — puts the wall at the door the request came
    through. The workspace now gets a record of what happened, after it has
    happened, and decides nothing.
    """

    async def _ask(tool: Tool, validated: Any, context: ToolContext) -> bool:
        args_dict = _validated_to_dict(validated)

        per_turn = _per_turn_set(session)
        h = args_fingerprint(tool.name, args_dict)
        if h in per_turn:
            log.info(
                "channel gate: already refused this turn on %s/%s "
                "(tool=%s hash=%s) — not asking again",
                channel, chat_id, tool.name, h,
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

        # Registered BEFORE the question is delivered. The operator can tap
        # faster than this coroutine resumes, and a future created afterwards
        # would miss that tap and park the chat until the timeout.
        loop = asyncio.get_running_loop()
        prompt_id = f"ask_{uuid.uuid4().hex[:12]}"
        entry = PendingAsk(
            event_id=prompt_id,
            chat_id=str(chat_id),
            tool_name=tool.name,
            args_hash=h,
            future=loop.create_future(),
        )
        pending_asks[prompt_id] = entry

        try:
            delivered = await ask_on_channel(prompt_id, tool.name, args_dict, ask_reason)
        except Exception:
            log.exception(
                "channel gate: asking %s on %s/%s raised", tool.name, channel, chat_id
            )
            delivered = False
        if not delivered:
            # Nobody was asked, so nobody can answer. This is the failure the
            # workspace fallback used to paper over — and papering over it is
            # how a dead approval button went unnoticed for three months.
            pending_asks.pop(prompt_id, None)
            per_turn.add(h)
            log.warning(
                "channel gate: could not deliver the question for %s to %s/%s "
                "— refusing instead of waiting on a prompt that was never shown",
                tool.name, channel, chat_id,
            )
            return False

        log.info(
            "channel gate: asked %s for %s/%s tool=%s — waiting up to %ss",
            prompt_id, channel, chat_id, tool.name, decision_timeout_s,
        )

        try:
            # `shield` for the reason `ask_gate.py` documents at length: bare
            # `wait_for` cancels the future it is waiting on, so a decision
            # landing at the boundary instant would be discarded instead of
            # honoured.
            approved = await asyncio.wait_for(
                asyncio.shield(entry.future), timeout=decision_timeout_s,
            )
        except asyncio.TimeoutError:
            approved = False
            log.info(
                "channel gate: no decision on %s within %ss — refusing",
                prompt_id, decision_timeout_s,
            )
        except asyncio.CancelledError:
            # The turn task died under us (bridge shutdown, chat cancel).
            # Drop the registration so a later tap reports "expired" rather
            # than resolving a future nothing is awaiting.
            pending_asks.pop(prompt_id, None)
            raise
        finally:
            pending_asks.pop(prompt_id, None)

        if not approved:
            # Only a refusal suppresses the rest of the turn. An approved
            # call that the model repeats is a new question and gets asked
            # again — the operator is answering in real time now.
            per_turn.add(h)

        _record_outcome(
            event_store,
            channel=channel,
            chat_id=chat_id,
            display_name=display_name,
            session_id=context.session_id or session.session_id,
            tool_name=tool.name,
            args=args_dict,
            args_hash=h,
            reason=ask_reason,
            transcript_tail=transcript_tail,
            posture_source=context.posture_source,
            prompt_id=prompt_id,
            approved=approved,
        )

        log.info(
            "channel gate: %s %s for %s/%s tool=%s",
            "approved" if approved else "refused",
            prompt_id, channel, chat_id, tool.name,
        )
        return approved

    return _ask


def _record_outcome(
    event_store: EventStore | None,
    *,
    channel: str,
    chat_id: str,
    display_name: str,
    session_id: str,
    tool_name: str,
    args: dict[str, Any],
    args_hash: str,
    reason: str,
    transcript_tail: list[dict[str, Any]],
    posture_source: str,
    prompt_id: str,
    approved: bool,
) -> None:
    """Write what happened to the workspace, after it has happened.

    A record, not a question — it lands already decided, so nothing in the
    inbox offers a verdict on a call that has already run or already been
    refused. Best-effort by design: the operator has answered on the channel
    and the turn is moving, so a store that cannot be written must not turn a
    completed decision into a failure. `approvals.jsonl` is the durable ledger.
    """
    if event_store is None:
        return
    verdict = "approved" if approved else "refused"
    try:
        event = WorkspaceEvent.new(
            kind="agent_post",
            source="agent",
            title=f"{tool_name} {verdict} on {channel}",
            summary=(f"{display_name} ({chat_id}): {reason}".rstrip().rstrip(":")),
            payload={
                "channel": channel,
                "chat_id": chat_id,
                "session_id": session_id,
                "tool": tool_name,
                "args": args,
                "args_hash": args_hash,
                "reason": reason,
                "transcript_tail": transcript_tail,
                "posture_source": posture_source,
                "prompt_id": prompt_id,
                "decision": verdict,
                # Read by the inbox to render this as history rather than as
                # something to act on. The decision surface is the channel.
                "decided_on_channel": True,
            },
            priority=3,
            author_id=f"{channel}:{chat_id}",
            author_display=display_name,
        )
        event_store.append_event(event)
        event_store.update_event_status(
            event.event_id, verdict if approved else "rejected",
            reason=f"{channel}_gate_decision",
        )
    except Exception:
        log.exception(
            "channel gate: could not record the %s of %s", verdict, tool_name
        )


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
