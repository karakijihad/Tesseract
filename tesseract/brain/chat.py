"""the assistant chat session — message history + streaming + tool-call loop.

Holds conversation state and delegates token generation to the model
adapter. When the model emits tool calls, they are executed via the
registered `ToolRegistry`, results are appended to history, and the loop
re-enters the adapter until the model returns a plain text stop or the
iteration cap is hit.

Streaming, tool calls, and stop reasons all bubble up as StreamChunk
events so the caller (REPL, WebSocket, etc.) can render as it sees fit.
Tool execution produces a synthesized `TOOL_RESULT` chunk so the UI can
show what happened.

The message history format is OpenAI-native (role:assistant with
tool_calls[], role:tool with tool_call_id). The OpenAI adapter passes
these through as-is. The Gemini adapter currently only handles text;
falling over to Gemini mid-tool-loop will error until message
translation is added in a later session.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections import deque
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Awaitable, Callable

from tesseract.brain.auto_recall import auto_recall, format_recall_block, load_auto_recall_config
from tesseract.brain.compaction import RUNNING_SUMMARY_PREFIX, compact_history
from tesseract.brain.completion_store import CompletionRecord, record_from_handle
from tesseract.brain.cost import BudgetExhausted, CostLedger, CostUsage
from tesseract.brain.memory_suggestion import MemorySuggestion, format_for_injection
from tesseract.brain.spawns import SpawnRegistry
from tesseract.orchestrator.agent_controller.interactive.registry import InteractiveSessionRegistry
from tesseract.brain.tools import AskFn, ToolRegistry, execute_tool
from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ChunkType,
    ModelAdapter,
    StreamChunk,
)
from tesseract.kernel.state import ToolCall
from tesseract.kernel.tools.base import ToolContext, ToolResult
from tesseract.kernel.tools.untrusted_envelope import (
    is_wrapped as _is_envelope_wrapped,
    wrap as _wrap_untrusted,
)
from tesseract.permissions.policy import PermissionPolicy

logger = logging.getLogger(__name__)

DEFAULT_COMPACT_THRESHOLD = 0.40  # fallback when roles.yaml omits it
DEFAULT_KEEP_RECENT_TURNS = 10
# CR-0 (2026-05-22) — sliding window with head anchor + token-budgeted tail.
# `head_anchor_messages` = first N USER messages kept verbatim across every
# compaction (StreamingLLM-style attention sink). `active_window_tokens` is
# the token budget for the verbatim tail; None falls back to
# `keep_recent_turns` (legacy message-count behavior).
# `summary_char_budget` caps the running [Context from earlier...] block —
# oldest `# Slice N` block is dropped first when over.
DEFAULT_HEAD_ANCHOR_MESSAGES = 3
DEFAULT_ACTIVE_WINDOW_TOKENS: int | None = None
DEFAULT_SUMMARY_CHAR_BUDGET = 8_000
PENDING_SUGGESTION_CAP = 8
PENDING_CONSCIENCE_CAP = 4
# Finished-background-spawn results awaiting next-turn delivery. UNBOUNDED in
# count on purpose: this was a `deque(maxlen=8)` whose own comment admitted
# "if many spawns finish before the assistant next acts, the oldest notices drop" —
# which is the "run N lanes and it all goes to garbage" report, in a constant.
# N dispatches deliver N results. Pressure is answered by compressing what is
# delivered (and saying so), never by discarding a result nothing will
# mention again. Each entry is already TokenJuice-compressed, the list drains
# every turn, and the per-session spawn cap bounds how many can be in flight.
SPAWN_COMPLETION_DELIVERY_BUDGET_CHARS = 24_000
# Floor per result when the budget forces a trim, so a large fan-out still
# leaves every finding legible rather than a page of ellipses.
SPAWN_COMPLETION_MIN_CHARS = 400

# 2026-05-17 — hard cap on the assembled prompt sent to ANY adapter in
# the chain. Codex CLI errors at 1_048_576 chars (`input_too_large`);
# leaving ~150 KB headroom for adapter wrapping + output token room.
# `_trim_to_budget` enforces this once at the chokepoint
# (`_messages_for_turn`) so codex, gpt-5.5, gpt-5.4-nano, gemini-2.5-flash
# all receive a payload that fits. See the 2026-05-17 incident on a
# Telegram chat: the prompt grew to 1.88 MB and exhausted every chain
# entry. Trim order: oldest history → recall_context
# sub-sections → never the system prompt or the last 3 turns.
PROMPT_CHAR_BUDGET = 900_000
KEEP_LAST_TURNS = 3
# Hard floor on the recall_context content kept inside the latest user
# message. If trimming below this would be required, drop the block
# entirely rather than emit a useless single-line stub.
RECALL_CONTEXT_MIN_KEEP = 4_000
# Every retry loop has a circuit breaker. The
# tool-iteration cap (`max_tool_iterations`) and adapter-error breaker
# (`max_consecutive_adapter_errors`) are **YAML-driven** — canonical values
# live in `roles.yaml::roles.chat_brain.{tool_iteration_cap,
# consecutive_error_cap}` and reach ChatSession via `boot.ChatBrainConfig`.
# No module constants here on purpose — single source of truth.


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── CR-0 compaction helpers ──────────────────────────────────────────

_SLICE_HEADER_RE = re.compile(r"^# Slice (\d+)\b", re.MULTILINE)


def _find_head_anchor_end(history: list[dict[str, Any]], n_msgs: int) -> int:
    """Return the index after which the head anchor ends.

    The head anchor is the slice of ``history`` up to and including the
    ``n_msgs``-th *non-summary*, *non-mid-turn* user message. Returns
    ``len(history)`` when ``history`` has fewer than ``n_msgs + 1``
    such messages.
    """
    if n_msgs <= 0:
        return 0
    user_count = 0
    for i, msg in enumerate(history):
        if msg.get("role") != "user":
            continue
        if _is_running_summary_message(msg):
            continue
        if msg.get("_mid_turn"):
            continue
        user_count += 1
        if user_count > n_msgs:
            return i
    return len(history)


def _is_running_summary_message(msg: dict[str, Any]) -> bool:
    """True if ``msg`` is the synthetic running-summary message produced
    by ``ChatSession.compact``. Detected by role + content prefix."""
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    return isinstance(content, str) and content.startswith(RUNNING_SUMMARY_PREFIX)


def _next_slice_number(prior_summary: str | None) -> int:
    """Return the next slice ordinal to use.

    Uses ``max(existing) + 1`` rather than ``count + 1`` so trimming the
    oldest block via :func:`_trim_summary_to_budget` does not produce
    duplicate slice numbers (the count would shrink after a drop;
    max preserves monotonic numbering across the session).
    """
    if not prior_summary:
        return 1
    highest = 0
    for match in _SLICE_HEADER_RE.finditer(prior_summary):
        try:
            n = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if n > highest:
            highest = n
    return highest + 1


def _trim_summary_to_budget(summary: str, budget_chars: int) -> str:
    """Drop oldest ``# Slice N`` blocks until ``summary`` fits the budget.

    Each block runs from one ``# Slice`` header up to (but not
    including) the next. If a single block exceeds the budget, that
    block is kept and the rest are dropped — losing a slice entirely
    is preferable to truncating a section mid-bullet.
    """
    if budget_chars <= 0 or len(summary) <= budget_chars:
        return summary
    headers = list(_SLICE_HEADER_RE.finditer(summary))
    if len(headers) <= 1:
        return summary  # nothing safe to drop
    # Drop oldest blocks one at a time until under budget.
    while len(headers) > 1 and len(summary) > budget_chars:
        drop_start = headers[0].start()
        next_start = headers[1].start()
        summary = summary[:drop_start] + summary[next_start:]
        headers = list(_SLICE_HEADER_RE.finditer(summary))
    return summary


# Chat-turn promise audit (codex audit-2 follow-up, 2026-05-19).
# Built after a Telegram confabulation incident where the assistant replied
# "Done. Every 15 minutes I'll fire a toast with a brief status summary"
# without invoking schedule_update. The model bump (gpt54_mini →
# gpt_oss_120b) is the primary defense; this audit is a backstop.
#
# Patterns intentionally narrow: only short, terminal action-claims
# that imply an external state change. Conversational use like
# "Done analyzing X" never matches (Done must end the clause).
import re as _re
_ACTION_VERBS = (
    r"created|set\s+up|setup|scheduled|enabled|configured|"
    r"added|registered|wired|installed|posted|sent|triggered|"
    r"pinged|saved|stored|logged|persisted|activated|started|"
    r"kicked\s+off|turned\s+on|switched\s+on"
)
_PROMISE_REGEX = _re.compile(
    r"(?ix)"
    r"(?:"
    # "Done." / "Done — ..." / "Done!" / "Done<eol>" — but NOT
    # "Done analyzing", "Done with X", "Done thinking". Anchored on
    # end-of-clause punctuation or end-of-string.
    r"  \b done \s* (?: [.!—–\-:]|$ )"
    r"  | \b all \s+ set \s* (?: [.!—–\-:]|$ )"
    # "I've" / "I have" / "I'm" / "I am" + already(?) + verb
    r"  | \b i \s* (?: [''’]ve | \s+ have | [''’]m | \s+ am ) \s+ "
    r"      (?: already \s+ )? "
    rf"      (?: {_ACTION_VERBS} )"
    # Bare past-tense terminal: "Scheduled." / "Enabled.\n" etc.
    rf"  | (?: ^ | [.!?\n] ) \s* (?: {_ACTION_VERBS} ) \s* (?: [.!—–\-]|$ )"
    r")"
)
_MAX_PROMISE_AUDIT_LOG_CHARS = 240

# Prompt assembly above this costs a visible slice of the voice latency budget
# and earns an INFO line; below it the measurement stays at DEBUG.
_SLOW_PROMPT_ASSEMBLY_S = 0.25


# Terminal-message guard. `<intent>` is a status channel, not reply content:
# the mirror parser routes it to `statusText` and never into the message body,
# so a turn whose terminal assistant message holds nothing but `<intent>`
# renders a status line above an empty bubble and returns cleanly — no error,
# no reply, no retry. Two recorded sessions ended exactly that way, both on a
# sentence promising the next action.
#
# The unclosed alternative is deliberate: a stream cut mid-tag leaves
# `<intent>...` with no terminator, and that is precisely the shape this guard
# has to catch.
_INTENT_BLOCK_RE = _re.compile(r"(?is)<intent>.*?(?:</intent>|\Z)")
_SURFACE_TAG_RE = _re.compile(r"(?i)</?(?:intent|spoken|answer)>")
_SURFACE_TAGS = (
    "<intent>", "</intent>", "<spoken>", "</spoken>", "<answer>", "</answer>",
)


def _strip_partial_tag_tail(text: str) -> str:
    """Drop a trailing fragment of a surface tag, e.g. the `<ans` left when a
    stream dies just after opening `<answer>`.

    The cockpit parser holds exactly this fragment back rather than rendering
    it, so counting it as visible text would let the shape this guard exists
    to catch slip through as a finished reply.
    """
    idx = text.rfind("<")
    if idx == -1:
        return text
    tail = text[idx:].lower()
    if any(tag.startswith(tail) for tag in _SURFACE_TAGS):
        return text[:idx]
    return text

# Substring markers rather than exact reasons: each provider spells truncation
# its own way and one of them hands us a stringified enum
# (`finishreason.max_tokens`).
#
# Only accidental cut-offs belong here. `refusal`, `content_filter`, `safety`
# and `recitation` are deliberate stops — the model or the provider declined —
# and the retry nudge tells the model to continue and call the tool it was
# about to call. Firing that at a refusal is the runtime pressuring a safety
# stop into compliance, so those reasons end the turn like any other reply and
# the operator reads the refusal. `stop_sequence` is out for the same family of
# reason: hitting a configured stop string is a clean ending.
_TRUNCATED_STOP_MARKERS = (
    "max_token", "max_output_token", "length", "incomplete",
)


def _has_operator_visible_text(assistant_text: list[str]) -> bool:
    """True if the accumulated assistant text carries anything the operator
    would actually read.

    `<intent>` blocks are stripped, then the remaining surface tags — so
    `<answer></answer>` counts as empty. Untagged prose counts as visible: the
    parser degrades it to `answer` and the operator sees it.

    Text-free input returns True. A turn that emitted nothing at all is a
    deliberate silence (the ambient observer declining to speak), not a
    dropped reply, and must not be nudged into talking.
    """
    try:
        body = "".join(assistant_text)
    except Exception:  # noqa: BLE001 — the guard must never break a turn
        return True
    if not body.strip():
        return True
    residue = _SURFACE_TAG_RE.sub("", _INTENT_BLOCK_RE.sub("", body))
    return bool(_strip_partial_tag_tail(residue).strip())


def _is_truncated_stop(stop_reason: str) -> bool:
    """True if the provider said this response was cut off rather than
    finished. Adapters normalise the two clean endings (`tool_use`,
    `end_turn`) and pass everything else through verbatim."""
    reason = (stop_reason or "").strip().lower()
    if not reason:
        return False
    return any(marker in reason for marker in _TRUNCATED_STOP_MARKERS)


def _audit_promise_without_action(
    *,
    assistant_text: list[str],
    turn_tool_invocations: int,
    options: Any,
    status_only_terminal: bool = False,
) -> None:
    """Log a WARNING if the final assistant text claims an action but
    no tool was invoked in the whole turn. Best-effort — failures here
    must never break the turn.

    `status_only_terminal` bypasses the turn-wide gate. A terminal message
    that is nothing but `<intent>` is a forward-looking promise by
    construction — "Reloading the corrected surface now" — so whatever tools
    ran earlier in the turn, that particular sentence was not acted on. The
    gate stays for every other shape, because "I've updated it" after a real
    `file_write` is not confabulation and logging it as such is noise.
    """
    if turn_tool_invocations > 0 and not status_only_terminal:
        return
    try:
        body = "".join(assistant_text).strip()
    except Exception:  # noqa: BLE001
        return
    if not body:
        return
    match = _PROMISE_REGEX.search(body)
    if match is None:
        return
    snippet = body.replace("\n", " ")
    if len(snippet) > _MAX_PROMISE_AUDIT_LOG_CHARS:
        snippet = snippet[:_MAX_PROMISE_AUDIT_LOG_CHARS] + "…"
    model = getattr(options, "model", "?")
    provider = getattr(options, "provider", "?")
    role = getattr(options, "role", "?")
    logger.warning(
        "chat: PROMISE_WITHOUT_ACTION — assistant claimed %r but no tool was invoked "
        "in this turn (role=%s provider=%s model=%s). Reply: %s",
        match.group(0), role, provider, model, snippet,
    )


def _flatten_message_text(content: Any) -> str:
    """Reduce a history-message ``content`` to plain text. Strings pass
    through; lists of message blocks (the multimodal shape) keep the
    ``text`` parts and drop everything else (image/file refs)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type") or ""
            if kind in ("text", "input_text") or "text" in block:
                text_val = block.get("text")
                if isinstance(text_val, str) and text_val.strip():
                    parts.append(text_val)
        return "\n".join(parts)
    return ""


def _collect_assistant_text(turn_tail: list[dict[str, Any]]) -> str:
    """Reduce post-user history (assistant + tool messages) to assistant
    final-text. Tool calls and tool-result rows are dropped; we keep
    only ``role: assistant`` content."""
    parts: list[str] = []
    for msg in turn_tail:
        if msg.get("role") != "assistant":
            continue
        text = _flatten_message_text(msg.get("content"))
        if text.strip():
            parts.append(text)
    return "\n\n".join(parts)


_RECALL_OPEN = "<recall_context>"
_RECALL_CLOSE = "</recall_context>"
_TRIM_HISTORY_MARKER = (
    "\n[history trimmed — older turns dropped to fit prompt budget]\n"
)
_TRIM_RECALL_MARKER = "\n[recall_context shortened]\n"


def _content_chars(msg: dict[str, Any]) -> int:
    """Char count of a message's content + any nested tool-call argument
    strings. Approximation — the adapter wraps these in JSON envelopes
    that add ~5% overhead. The budget headroom (148K char) absorbs that."""
    n = 0
    content = msg.get("content")
    if isinstance(content, str):
        n += len(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if isinstance(text, str):
                    n += len(text)
    for call in msg.get("tool_calls") or []:
        if isinstance(call, dict):
            args = (call.get("function") or {}).get("arguments")
            if isinstance(args, str):
                n += len(args)
    return n


def _shrink_recall_block(text: str, target_len: int) -> str:
    """Trim the inside of a ``<recall_context>...</recall_context>`` wrapper.

    Drops trailing content first (URL extracts, prior memory bodies and
    log-tail are assembled last by `_chat_memory.recall_for_inbound`).
    If shrinking below ``RECALL_CONTEXT_MIN_KEEP`` would be required,
    return the message without the recall block at all — a stub recall
    is worse than no recall (the model latches on to a single bullet
    and confabulates around it).
    """
    open_idx = text.find(_RECALL_OPEN)
    close_idx = text.find(_RECALL_CLOSE)
    if open_idx == -1 or close_idx == -1 or close_idx < open_idx:
        return text
    head = text[:open_idx]
    tail = text[close_idx + len(_RECALL_CLOSE):]
    inner = text[open_idx + len(_RECALL_OPEN):close_idx]
    overhead = len(head) + len(tail) + len(_RECALL_OPEN) + len(_RECALL_CLOSE) + len(_TRIM_RECALL_MARKER)
    keep = target_len - overhead
    if keep < RECALL_CONTEXT_MIN_KEEP:
        return (head + tail).strip("\n")
    inner_keep = inner[:keep].rstrip()
    return head + _RECALL_OPEN + inner_keep + _TRIM_RECALL_MARKER + _RECALL_CLOSE + tail


def _trim_to_budget(
    messages: list[dict[str, Any]],
    *,
    char_limit: int,
) -> list[dict[str, Any]]:
    """Enforce a hard char budget on the assembled message list.

    Drop order, most aggressive last:
      1. Oldest history entries (preserve system + last KEEP_LAST_TURNS).
      2. Shrink the ``<recall_context>`` block inside the latest user
         message.
      3. (Last resort) emit a warning — adapter will reject. We never
         drop the latest user message; that's the operator's actual ask.

    Idempotent on already-small payloads.
    """
    total = sum(_content_chars(m) for m in messages)
    if total <= char_limit:
        return messages

    keep_indices: set[int] = set()
    if messages and messages[0].get("role") == "system":
        keep_indices.add(0)
    for i in range(max(0, len(messages) - KEEP_LAST_TURNS), len(messages)):
        keep_indices.add(i)

    out: list[dict[str, Any]] = []
    dropped = 0
    for i, m in enumerate(messages):
        if i in keep_indices:
            out.append(m)
        else:
            dropped += 1
    if dropped:
        # Insert a marker right after the system message so the model
        # knows context was clipped (single line, low cost).
        # `role: assistant` (not `system`) — Gemini rejects multiple
        # system messages in one call and is in our fallback chain. An
        # assistant-role note reads naturally across all providers.
        insert_at = 1 if (out and out[0].get("role") == "system") else 0
        out.insert(
            insert_at,
            {"role": "assistant", "content": _TRIM_HISTORY_MARKER.strip()},
        )

    total = sum(_content_chars(m) for m in out)
    if total <= char_limit:
        return out

    # Step 2: shrink recall_context in the last user message.
    last = out[-1]
    content = last.get("content")
    if isinstance(content, str) and _RECALL_OPEN in content:
        budget_for_user = max(
            RECALL_CONTEXT_MIN_KEEP,
            char_limit - sum(_content_chars(m) for m in out[:-1]),
        )
        shrunk = _shrink_recall_block(content, target_len=budget_for_user)
        out[-1] = {**last, "content": shrunk}

    total = sum(_content_chars(m) for m in out)
    if total > char_limit:
        logger.warning(
            "prompt budget %d exceeded after trim: %d chars remaining",
            char_limit, total,
        )
    else:
        logger.info(
            "prompt trim: %d→%d chars (limit %d)",
            sum(_content_chars(m) for m in messages), total, char_limit,
        )
    return out


def _drain_workspace_comments(target_comment_id: str | None = None) -> tuple[list[str], list[str]]:
    """Read undelivered operator comments and return ``(blocks, comment_ids)``.

    Format: ``[workspace_comment_on_<event_id>] (cmt_<id>) {body}`` —
    the bracketed tag mirrors observer/conscience injection shape and
    gives the assistant the comment_id it needs to call ``workspace_reply``.

    Codex audit 2026-05-07 M1: when ``target_comment_id`` is supplied,
    return only the matching undelivered comment. The caller is
    expected to drive a synthetic turn per queued payload, so draining
    all undelivered items would make a successful reply on turn A
    silently mark unrelated items B, C delivered before their own
    turns run.

    ``target_comment_id=None`` returns ([], []) — the helper is no
    longer used as a global flush; callers without a specific target
    have nothing to drain.

    Codex audit 2026-05-06 M2: this no longer marks the comments
    delivered. The caller is expected to call
    ``ChatSession.confirm_workspace_delivery`` once the synthetic
    turn's ``workspace_reply`` has succeeded. On cancel/error/no-reply
    the IDs drop without marking, so the next turn re-drains them and
    the operator's intent is not silently lost.

    Best-effort: store/path failures are non-fatal — the chat turn
    still proceeds, the comments will surface on a later tick.
    """
    if not target_comment_id:
        return [], []
    try:
        from tesseract.kernel.workspace_changes import workspace_events_dir
        from tesseract.workspace_events import EventStore
    except ImportError:
        return [], []
    try:
        store = EventStore(workspace_events_dir())
        pending = store.list_undelivered_operator_comments()
    except Exception:
        logger.exception("workspace comment drain failed")
        return [], []
    for c in pending:
        if c.comment_id != target_comment_id:
            continue
        block = (
            f"[workspace_comment_on_{c.event_id}] (cmt_id={c.comment_id}) {c.body}"
        )
        return [block], [c.comment_id]
    return [], []


def _drain_operator_posts(target_event_id: str | None = None) -> tuple[list[str], list[str]]:
    """Read undelivered ``operator_post`` events and return ``(blocks, event_ids)``.

    Format: ``[workspace_post_on_<event_id>] {title} — {body}``. The assistant is
    expected to reply via ``workspace_reply`` (with ``comment_id`` left
    as the originating event_id, since there's no comment yet — the
    workspace_reply directive in `prompt.py` covers both shapes).

    Codex audit 2026-05-07 M1: when ``target_event_id`` is supplied,
    return only the matching undelivered ``operator_post``.
    ``target_event_id=None`` returns ([], []) — see
    ``_drain_workspace_comments`` for the rationale.

    Same fail-soft and deferred-delivery contract as
    ``_drain_workspace_comments`` (Codex audit 2026-05-06 M2).
    """
    if not target_event_id:
        return [], []
    try:
        from tesseract.kernel.workspace_changes import workspace_events_dir
        from tesseract.workspace_events import EventStore
    except ImportError:
        return [], []
    try:
        store = EventStore(workspace_events_dir())
        pending = store.list_undelivered_operator_posts()
    except Exception:
        logger.exception("workspace operator_post drain failed")
        return [], []
    for ev in pending:
        if ev.event_id != target_event_id:
            continue
        body = str((ev.payload or {}).get("body") or ev.summary or "").strip()
        title = ev.title.strip() or "(untitled)"
        block = f"[workspace_post_on_{ev.event_id}] {title} — {body}"
        return [block], [ev.event_id]
    return [], []


def _mark_workspace_delivered(comment_ids: list[str], event_ids: list[str]) -> None:
    """Persist the delivered flags for IDs drained earlier in this turn.

    Best-effort: a store failure logs and returns; the next drain will
    simply re-include the same items.
    """
    if not comment_ids and not event_ids:
        return
    try:
        from tesseract.kernel.workspace_changes import workspace_events_dir
        from tesseract.workspace_events import EventStore
    except ImportError:
        return
    try:
        store = EventStore(workspace_events_dir())
    except Exception:
        logger.exception("workspace delivery mark: store init failed")
        return
    for cid in comment_ids:
        try:
            store.mark_comment_delivered(cid)
        except Exception:
            logger.exception("workspace mark_comment_delivered failed")
    for eid in event_ids:
        try:
            store.mark_event_delivered(eid)
        except Exception:
            logger.exception("workspace mark_event_delivered failed")


def _format_conscience_transition(transition: dict[str, Any]) -> str:
    """Render a drift transition as a synthetic user-visible note.

    Keeps the same `[tag] body` shape observer suggestions use so the
    model recognises it as a system-originated aside, not a real user
    turn. Short by design — don't spam the prompt with a full signal
    dump; the assistant can call `conscience_status` for detail.

    When the heartbeat enriched the transition with a `recurrence_days`
    map (counts in 30/90/365-day windows) the note surfaces those
    counts so the assistant feels temporal patterns ("3rd time this month")
    rather than treating each drift as a one-off. A short reflection
    prompt invites him to call `memory_save` with one line on *why* —
    structured record + natural-language reason together is what makes
    the loop actionable.
    """
    frm = transition.get("from", "unknown")
    to = transition.get("to", "unknown")
    summary = transition.get("summary") or {}
    ok = int(summary.get("ok", 0))
    warn = int(summary.get("warn", 0))
    bad = int(summary.get("bad", 0))
    changed = transition.get("changed_signals") or []
    changed_lines = [
        f"{c.get('name', '?')}: {c.get('from', '?')}→{c.get('to', '?')}"
        + (f" ({c['detail']})" if c.get("detail") else "")
        for c in changed
    ]
    body = f"worst {frm} → {to}. {ok} ok · {warn} warn · {bad} bad."
    if changed_lines:
        body += " Changed: " + "; ".join(changed_lines) + "."
    if transition.get("flapping"):
        body += " (flapping — same-day band oscillation, collapsed into one entry)."
    recurrence = transition.get("recurrence_days") or {}
    if isinstance(recurrence, dict) and recurrence:
        body += " Recurrence: " + _format_recurrence(recurrence) + "."
    if transition.get("memory_id"):
        body += (
            " Recorded as memory; reflect on the cause in one line via memory_save"
            " (type=`conscience`, tag it `drift_reflection`) so the next drift can see it."
        )
    body += " Call conscience_status for detail."
    return f"[conscience_drift] {body}"


def _format_recurrence(recurrence: dict[Any, Any]) -> str:
    """Format `{30: 3, 90: 7, 365: 12}` as `3 in 30d · 7 in 90d · 12 in 365d`.

    Skips windows with zero hits so a first-ever drift doesn't read as
    a wall of zeros. JSON-decoded payloads carry string keys; coerce.
    """
    parts: list[str] = []
    try:
        items = sorted(((int(k), int(v)) for k, v in recurrence.items()), key=lambda x: x[0])
    except (TypeError, ValueError):
        return ""
    for window, count in items:
        if count <= 0:
            continue
        parts.append(f"{count} in {window}d")
    return " · ".join(parts) if parts else "first observed"


# MP-2 ambient observer: defence-in-depth secret redaction. The Mirror
# already redacts before sending, but the chat brain re-runs the same
# regex so a misbehaving caller (tests, external integrations) cannot
# leak token-shaped fields into the prompt.
_VIEW_SNAPSHOT_SECRET_RE = re.compile(
    r"(token|secret|password|api_?key|bot_?token)", re.IGNORECASE
)


def _redact_view_snapshot(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: ("[redacted]" if _VIEW_SNAPSHOT_SECRET_RE.search(k) else _redact_view_snapshot(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_view_snapshot(v) for v in value]
    return value


def _format_view_snapshot(snapshot: dict[str, Any]) -> str:
    """Render a Mirror view-context snapshot as a one-shot system aside.

    Shape from the Mirror:
    ``{"view": "<id>", "view_state": {...}, "layers": {...}}``.
    Output format the chat brain can read at a glance:
        ``[current_view] <id>\\n[view_state] <json>\\n[layers] <json>``
    Empty ``view`` is treated as "no snapshot" — the block is omitted.

    ``layers`` is optional and older clients do not send it — the frontend
    ships compiled into the app and updates on a different cadence to this
    file, so its absence is a version skew, not an error.
    """
    view = snapshot.get("view")
    if not isinstance(view, str) or not view:
        return ""
    raw_state = snapshot.get("view_state")
    state = raw_state if isinstance(raw_state, dict) else {}
    redacted = _redact_view_snapshot(state)
    try:
        rendered = json.dumps(redacted, sort_keys=True)
    except (TypeError, ValueError):
        rendered = "{}"
    block = f"[current_view] {view}\n[view_state] {rendered}"
    raw_layers = snapshot.get("layers")
    if isinstance(raw_layers, dict) and raw_layers:
        try:
            layers = json.dumps(_redact_view_snapshot(raw_layers), sort_keys=True)
        except (TypeError, ValueError):
            layers = ""
        if layers:
            block += f"\n[layers] {layers}"
    return block


def _summarize_spawn(handle: Any) -> str:
    """Short human-readable line for a completed background spawn, for the
    UI's SPAWN_DONE chunk. Read from the asyncio.Task — failure exception
    type if any, otherwise the first line of the result.

    This is a LABEL, not a delivery. What reaches the model is
    `_format_spawn_completion`, which carries the result itself; a first
    line capped at 160 chars is what left the assistant holding a handle and no
    finding."""
    try:
        if handle.task.cancelled() or handle.cancelled:
            return "cancelled"
        exc = handle.task.exception()
        if exc is not None:
            return f"{type(exc).__name__}: {exc}"
        result = handle.task.result()
        text = getattr(result, "output", "") or ""
        first_line = text.strip().splitlines()[0] if text.strip() else "(empty)"
        if len(first_line) > 160:
            first_line = first_line[:160] + "…"
        return first_line
    except Exception:
        return "(unsummarizable)"


def _spawn_rule_name(kind: str) -> str:
    """The TokenJuice rule family a spawn's output should be compressed by.

    Spawn kinds carry a target suffix (`lane_turn:coder/claude`,
    `agent:vault_librarian`); rules match on bare tool names."""
    return (kind or "").split(":", 1)[0]


def _format_completion_record(record: CompletionRecord) -> str:
    """Render a finished background spawn as a one-shot injection block —
    carrying the RESULT, not a pointer to it.

    The old block delivered the first line of the output capped at 160
    characters plus the handle, so the assistant learned only that something had
    finished and had to remember an opaque id, decide to fetch, and spend a
    second tool call to learn what. Models are bad at all three. The result
    now travels in the block, compressed through the same TokenJuice rules
    the tool path uses — which for lane and delegate output is head+tail,
    preserving an auditor's verdict at the tail. `spawn_await` is now the
    exception, for genuinely huge output.

    Takes the durable record rather than the handle so a completion replayed
    after a restart — when the handle is long gone — renders byte-identically
    to one delivered live. Queued by ``ChatSession.ingest_spawn_completion``
    or ``replay_undelivered_completions`` and surfaced on the next turn's
    iteration 0 (same one-shot semantics as conscience notes).
    """
    from tesseract.brain.tools import compress_for_delivery

    status = record.status
    kind = record.kind
    body, compressed = compress_for_delivery(
        record.output, _spawn_rule_name(kind)
    )
    # A delegate's output is whatever a CLI read out of a repository, a web
    # page, or a compromised model — and this block is injected as a
    # role=user message, not a tool result, so it misses the wrapping
    # `_run_pending_calls` does for untrusted tools. Delivering the whole
    # result rather than a 160-char line is what makes that matter: one
    # truncated line is a poor injection vector, kilobytes of attacker-shaped
    # text is not.
    body = _wrap_untrusted(tool=kind or "spawn", output=body)
    handle_id = record.handle_id
    header = f"[spawn_completed] handle={handle_id} kind={kind} status={status}"
    footer = f"[end of {handle_id}]"
    if compressed:
        footer = (
            f"[end of {handle_id} — shortened; spawn_await this handle "
            f"for the untrimmed output]"
        )
    return f"{header}\n{body}\n{footer}"


def _format_spawn_completion(handle: Any) -> str:
    """Snapshot a live handle and render its delivery block."""
    return _format_completion_record(record_from_handle(handle))


def _head_tail(block: str, budget: int) -> str:
    """Keep the opening and closing lines of `block` within `budget` chars.

    Whole lines, like the TokenJuice `head_tail` reducer this sits beside —
    slicing at raw character offsets cuts mid-word and reads as corruption
    rather than as elision."""
    lines = block.splitlines()
    if not lines:
        return block
    # The first line carries the handle id and status. Keep it whatever the
    # budget says — a trimmed block nobody can attribute to a dispatch is
    # the drop this whole path exists to prevent, wearing an ellipsis.
    head: list[str] = [lines[0]]
    tail: list[str] = []
    used = len(lines[0]) + 1
    front, back = 1, len(lines) - 1
    while front <= back:
        # Alternate ends so the header and the verdict both survive.
        nxt = lines[front] if len(head) <= len(tail) else lines[back]
        if used + len(nxt) + 1 > budget:
            break
        if len(head) <= len(tail):
            head.append(nxt)
            front += 1
        else:
            tail.insert(0, nxt)
            back -= 1
        used += len(nxt) + 1
    if front > back:
        return block
    return "\n".join([*head, "…", *tail])


def _fit_spawn_completions(blocks: list[str]) -> list[str]:
    """Fit N delivered results into the delivery budget without losing one.

    Over budget, every block is trimmed head+tail to an equal share (never
    below `SPAWN_COMPLETION_MIN_CHARS`) and each says it was shortened. The
    one thing this never does is return fewer blocks than it was given: a
    result that is never mentioned is a result the assistant cannot know to go and
    fetch, and that is the failure mode the whole change exists to remove."""
    if not blocks:
        return []
    total = sum(len(b) for b in blocks)
    if total <= SPAWN_COMPLETION_DELIVERY_BUDGET_CHARS:
        return blocks
    share = max(
        SPAWN_COMPLETION_MIN_CHARS,
        SPAWN_COMPLETION_DELIVERY_BUDGET_CHARS // len(blocks),
    )
    trimmed = 0
    out: list[str] = []
    for block in blocks:
        if len(block) <= share:
            out.append(block)
            continue
        trimmed += 1
        out.append(_head_tail(block, share))
    if trimmed:
        out.append(
            f"[{trimmed} of {len(blocks)} completed dispatches were shortened "
            f"to fit this turn — every one is above, and spawn_await on a "
            f"handle returns its untrimmed output]"
        )
    return out


def _format_spawn_stall(handle: Any) -> str:
    """Render a stuck-running background spawn as a one-shot injection block.

    Queued by ``ChatSession.ingest_spawn_stall`` when the halt-watchdog
    (``SpawnRegistry.sweep_stalled``) flags a spawn still ``running`` past the
    configured bound. Surfaced like ``[spawn_completed]`` so the assistant can decide to
    ``spawn_cancel`` (and retry) or ``spawn_await`` if it wants to keep waiting.
    """
    return (
        f"[spawn_stalled] handle={handle.handle_id} kind={handle.kind} "
        f"started_at={handle.started_at} — still running well past the expected "
        f"bound; it may be wedged. Consider spawn_cancel (then retry) or "
        f"spawn_await if you want to keep waiting."
    )


def _format_spawn_lost(handle_id: str, kind: str, was_parked: bool = False) -> str:
    """Render a spawn orphaned by a backend restart as a one-shot injection block.

    P6 Task 3 §G5. Queued by ``ChatSession.mark_vanished_spawns`` when the
    resume-time journal sweep (``spawn_journal.sweep_orphans``) finds a
    ``start`` event with no matching ``terminal`` event — the owning
    ``asyncio.Task`` died with the old process, so there is nothing to
    ``spawn_await`` or ``spawn_cancel``. No cross-restart resumption:
    vanished means failed, period. ``was_parked`` (journal ``parked`` event,
    trio W4) distinguishes a spawn that died holding an unanswered operator
    ASK — actionable: re-dispatch may just need the operator present.
    """
    if was_parked:
        return (
            f"[spawn_lost] handle={handle_id} kind={kind} — was parked "
            f"awaiting operator input when the backend restarted; treat as "
            f"failed (nothing to await or cancel). Its permission ask was "
            f"never answered — re-dispatching may work with the operator "
            f"present."
        )
    return (
        f"[spawn_lost] handle={handle_id} kind={kind} — vanished when the "
        f"backend restarted; treat as failed (nothing to await or cancel)."
    )


@dataclass
class ChatSession:
    adapter: ModelAdapter
    system_prompt: str
    # Per-session tunables — required, **YAML-driven** (no module constants).
    # Sourced from `roles.yaml::roles.chat_brain.{tool_iteration_cap,
    # consecutive_error_cap}` via `boot.ChatBrainConfig`. Live-updated by
    # the config watcher in `rebuild_adapters` so external YAML edits reflect
    # on existing sessions. Tests that construct ChatSession directly must
    # pass these explicitly — the canonical values are config, not code.
    max_tool_iterations: int
    max_consecutive_adapter_errors: int
    options: AdapterOptions = field(default_factory=AdapterOptions)
    history: list[dict[str, Any]] = field(default_factory=list)
    registry: ToolRegistry | None = None
    tool_context: ToolContext = field(default_factory=ToolContext)
    compact_threshold: float = DEFAULT_COMPACT_THRESHOLD
    keep_recent_turns: int = DEFAULT_KEEP_RECENT_TURNS
    # Sliding-window knobs. See the module docstring
    # phase-CR-0-compaction-sliding-window.md.
    head_anchor_messages: int = DEFAULT_HEAD_ANCHOR_MESSAGES
    active_window_tokens: int | None = DEFAULT_ACTIVE_WINDOW_TOKENS
    summary_char_budget: int = DEFAULT_SUMMARY_CHAR_BUDGET
    ask_fn: AskFn | None = None  # operator approval callback for ASK-permission tools
    policy: PermissionPolicy | None = None  # config-driven per-tool posture
    # When set, called per turn to re-assemble the system prompt — lets
    # SOUL.md edits land inside the active session instead of
    # waiting for the next one. If unset, the frozen `system_prompt` is used.
    # CR-3 — channel sessions plumb a channel-aware builder here from
    # ``_build_chat_session``; the channel overlay is inlined inside the
    # assembled manifest (cacheable prefix) rather than appended at runtime.
    prompt_builder: Callable[[], str] | None = None
    # CR-3 observability — set to ``"channel"`` for sessions built by a
    # channel adapter, otherwise ``"cockpit"``. Not load-bearing for the
    # prompt (the builder already encodes the overlay); CR-5 reads this
    # to choose between operator-attended ASK and the workspace-nudge
    # gate. ``channel_display_name`` mirrors the value handed to the
    # channel-aware ``prompt_builder`` for the same reason.
    session_kind: str = "cockpit"
    channel_display_name: str | None = None
    # Spawn halt-watchdog bound (Stage 2B). When set, each turn sweeps the
    # spawn registry for background spawns stuck `running` past this many
    # seconds and queues a one-shot `[spawn_stalled]` note. None disables the
    # watchdog (REPL / sub-agent / test sessions). Sourced from
    # `runtime.yaml::spawn_stall_seconds` via `_build_chat_session`.
    spawn_stall_seconds: float | None = None
    # per-session cap on simultaneously-running background
    # spawns. None disables the cap (REPL / sub-agent / test sessions).
    # Sourced from `runtime.yaml::max_concurrent_spawns_per_session` via
    # `_build_chat_session`; pushed onto the registry in `__post_init__`.
    spawn_max_concurrent: int | None = None
    # Shared cost accountant. When present, preflight runs once per send()
    # (blocks tier: api when cap hit) and record() runs per STOP (every
    # tool-loop iteration bills separately, matching real OpenAI billing).
    # None = cost tracking disabled for this session (tests, sub-agent sessions).
    cost_ledger: CostLedger | None = None
    # Cost UX overhaul: operator-approval callback when chat preflight
    # raises `BudgetExhausted`. Signature mirrors `AskFn` but for cost
    # scopes. None = old behavior (yield error chunk, abort turn).
    # Mirror plumbs this in `session.py:create_server_session`.
    overage_ask_fn: Callable[[BudgetExhausted], Awaitable[bool]] | None = None
    _pending_suggestions: deque[MemorySuggestion] = field(
        default_factory=lambda: deque(maxlen=PENDING_SUGGESTION_CAP),
        repr=False,
    )
    _pending_conscience: deque[str] = field(
        default_factory=lambda: deque(maxlen=PENDING_CONSCIENCE_CAP),
        repr=False,
    )
    _pending_spawn_completions: deque[str] = field(
        default_factory=deque,
        repr=False,
    )
    # Handle ids of the queued spawn COMPLETIONS above (stalls and lost-spawn
    # notes have no durable record, so they never appear here). Claimed in the
    # completion store when the turn that drained them COMMITS, which is what
    # stops a restart from replaying a result the model has already read —
    # and, on any other outcome, what stops a dead turn from eating one.
    _queued_completion_ids: list[str] = field(default_factory=list, repr=False)
    # Drained into the current turn but not yet committed. The blocks are kept
    # verbatim (pre-fit) so a rollback re-queues the originals and a later
    # redelivery re-fits them against whatever else has since arrived. Caller
    # (`turn_runner._run_turn`) invokes `confirm_spawn_delivery` on a clean
    # stream and `rollback_spawn_delivery` on cancel / error — the same shape
    # workspace comments already use one method over.
    _delivering_spawn_blocks: list[str] = field(default_factory=list, repr=False)
    _delivering_completion_ids: list[str] = field(default_factory=list, repr=False)
    _observed_ids: deque[str] = field(
        default_factory=lambda: deque(maxlen=500), repr=False
    )
    # Drained from _pending_suggestions at send() start; stable across all
    # tool-loop iterations; cleared in finally. Not counted by should_compact.
    _turn_injection: str = field(default="", repr=False)
    _observer_subscriber: Any | None = field(default=None, repr=False)
    _observer_last_index: int = field(default=0, repr=False)
    # Adapter-error circuit breaker (Layer 2, 2026-05-05). Bumped on each
    # ERROR chunk from the chat_brain chain, reset on any successful STOP.
    # When it crosses `self.max_consecutive_adapter_errors` the outer send()
    # loop surfaces a final ERROR and stops retrying.
    _consecutive_adapter_errors: int = field(default=0, repr=False)
    # P6 Task 5 — escalate-on-failure reflex. Tracks the currently-running
    # consecutive-error streak for a single tool name within THIS turn
    # (reset at each `send()` start, below). At ≥2, `_run_pending_calls`
    # records `(name, count)` into `failures_signal` so the next prompt's
    # digest carries an "escalate now" line; cleared (both here and in
    # `failures_signal`) the moment that same tool succeeds.
    _tool_error_streak_name: str = field(default="", repr=False)
    _tool_error_streak_count: int = field(default=0, repr=False)
    # Whole-phase review fix (2026-07-06) — the `failures_signal` scope key.
    # Deliberately NOT `tool_context.session_id`: `fork_for_synthetic` below
    # copies `session_id` verbatim into a synthetic session's `ToolContext`
    # (by design — spawn journaling needs the parent's session id) but that
    # forked session runs CONCURRENTLY with the parent chat turn (see
    # `mirror/server/session.py::synthetic_turn_tasks`), so scoping on
    # `session_id` would let a fork's tool call clear or collide with the
    # parent's still-unresolved streak. `default_factory` mints a fresh id
    # per `ChatSession.__init__` call, so a fork (built via a fresh
    # `ChatSession(...)` call in `fork_for_synthetic`, not `copy.copy`) gets
    # its own scope automatically without any extra plumbing.
    _failures_scope_id: str = field(default_factory=lambda: uuid.uuid4().hex, repr=False)
    # Codex audit 2026-05-06 M2: IDs drained for the current synthetic
    # workspace turn but NOT yet marked delivered. Caller (Mirror ws.py)
    # invokes `confirm_workspace_delivery` after the synthetic turn's
    # `workspace_reply` succeeds, or `rollback_workspace_delivery` on
    # cancel/error/no-reply so the operator's intent is not silently lost.
    _pending_workspace_comment_ids: list[str] = field(default_factory=list, repr=False)
    _pending_workspace_event_ids: list[str] = field(default_factory=list, repr=False)
    # Phase 2 (CLI parity): operator-typed messages that arrive while a turn
    # is mid-flight. The WS appends here. The tool loop drains this list
    # between iterations (inside `send`, after
    # `_run_pending_calls` yields the last result chunk) and folds each
    # entry into history as a `role: user` message with `[mid-turn]`
    # framing so the model can pivot. A USER_INJECT StreamChunk is
    # yielded so the WS can fire `stream_user_inject` and clear the
    # frontend "queued" badge.
    pending_injected_messages: list[dict[str, Any]] = field(default_factory=list, repr=False)
    # Phase 4 (CLI parity): background-spawn registry. Tools that take
    # `await=False` (currently only delegate_coder — delegate_auditor
    # and invoke_agent follow in a separate pass) register an
    # asyncio.Task here and return immediately with the handle id.
    # `spawn_check` / `spawn_await` / `spawn_cancel` tools query this
    # via `tool_context.spawns` (wired in `__post_init__`).
    # `reset()` cancels every running spawn so `/reset` doesn't leave
    # orphaned subprocesses.
    spawns: SpawnRegistry = field(default_factory=SpawnRegistry, repr=False)
    # A7 — interactive PTY sessions (agent_controller). Cross-linked into
    # tool_context in __post_init__. `reset()` closes all open sessions.
    interactive_sessions: InteractiveSessionRegistry = field(
        default_factory=InteractiveSessionRegistry, repr=False
    )
    # Lean-agent-os P1 Task 2 — extended-tool names `tool_search` has
    # surfaced this session. Starts empty (core-only schemas at session
    # start); survives across turns within the session, cleared only by
    # constructing a fresh ChatSession (e.g. `fork_for_synthetic`).
    _enabled_extended_tools: set[str] = field(default_factory=set, repr=False)
    # Auto-recall cross-turn dedup (review fix on lean-agent-os P1 Task 3).
    # One entry per turn that ran auto_recall, holding the memory ids
    # injected that turn; trimmed to `auto_recall.dedup_window_turns`
    # (config, re-read every turn) after each append. A memory id present
    # anywhere in this window is excluded from re-injection. In-memory
    # only — never persisted, cleared only by constructing a fresh
    # ChatSession.
    _recall_dedup_window: deque[set[str]] = field(default_factory=deque, repr=False)

    def __post_init__(self) -> None:
        # Cross-link the spawn registry into ToolContext so tools can
        # reach it without importing brain-layer modules. Runs once at
        # ChatSession construction; `tool_context` is the same instance
        # for the session's lifetime, so this assignment doesn't get
        # invalidated by `rebuild_adapters` etc.
        self.tool_context.spawns = self.spawns
        self.tool_context.interactive_sessions = self.interactive_sessions
        self.tool_context.enabled_extended_tools = self._enabled_extended_tools
        # Push-on-completion: a finished background spawn queues a next-turn
        # notice so the assistant sees it instead of relying on a poll it may never make.
        self.spawns.completion_notifier = self.ingest_spawn_completion
        # P6 Task 3 §G5 — thread the Mirror session_id (if any) into the spawn
        # registry so it can journal start/terminal events for resume-time
        # orphan detection. Additive: `tool_context.session_id` defaults to
        # `""` (REPL / sub-agent sessions never set it), which leaves
        # `spawns.session_id` `None` and journaling disabled there.
        self.spawns.session_id = self.tool_context.session_id or None
        # Durable completion records are attributed to the principal this
        # session acts for; empty means the assistant's own work (the operator).
        self.spawns.owner_principal = self.tool_context.caller_principal
        self.spawns.max_concurrent = self.spawn_max_concurrent
        # M5 — publish the concurrent cap onto the context so sub-agent
        # sessions built from a copy of it (agent_factory) inherit the same
        # fan-out limit instead of running uncapped.
        if self.spawn_max_concurrent is not None:
            self.tool_context.spawn_max_concurrent = self.spawn_max_concurrent
        # trio W3 — spawn-depth backstop: the context carries the session's
        # nesting level (root 0; agent_factory bumps sub-sessions) + the
        # runtime.yaml cap; the registry enforces at register().
        self.spawns.depth = self.tool_context.spawn_depth
        self.spawns.max_depth = self.tool_context.spawn_depth_cap

    def _recall_excluded_ids(self) -> set[str]:
        """Union of memory ids injected within the current dedup window."""
        excluded: set[str] = set()
        for turn_ids in self._recall_dedup_window:
            excluded |= turn_ids
        return excluded

    def _record_recall_injection(self, memory_ids: set[str], window_turns: int) -> None:
        """Append this turn's injected ids and trim to `window_turns`.

        Appends unconditionally (including an empty set on a no-recall
        turn) so the window tracks elapsed turns, not just turns that
        injected something.
        """
        self._recall_dedup_window.append(memory_ids)
        while len(self._recall_dedup_window) > window_turns:
            self._recall_dedup_window.popleft()

    def fork_for_synthetic(
        self,
        *,
        synthetic_excluded_tools: tuple[str, ...] = ("set_mood", "set_state"),
    ) -> "ChatSession":
        """Return an ephemeral ChatSession for a synthetic workspace turn.

        WP-2: a synthetic workspace turn runs concurrently with the chat
        turn. It must NOT mutate the canonical history (the workspace
        reply is delivered via the `workspace_reply` tool, not via the
        chat conversation), and it must NOT share mutable state with the
        chat session — see
        the per-tool / per-infra audit that drove these decisions.

        Shared (read-only or natively concurrency-safe):
          - ``adapter`` underlying chain (stateless wrappers around
            HTTP/subprocess); the FallbackAdapter wrapper is **forked**
            so synthetic-turn failures don't trip chat-turn breakers.
          - ``system_prompt``, ``prompt_builder`` — pure read.
          - ``ask_fn``, ``overage_ask_fn``, ``policy``, ``cost_ledger`` —
            shared infrastructure; their own locks handle concurrency.

        Fresh (private to the synthetic turn):
          - ``history`` — deep copy snapshot at fork time. Never written
            back. The synthetic turn appends locally; on completion the
            ephemeral session is dropped.
          - ``tool_context`` — fresh ``cancel_event`` so cancelling the
            synthetic turn doesn't cancel the chat turn, fresh ``todos``
            so the synthetic turn can't mutate the operator's checklist,
            fresh ``spawns`` so synthetic-launched subprocesses don't
            mingle with chat spawns.
          - ``ToolRegistry`` — copy minus ``synthetic_excluded_tools``
            (default: ``set_mood``, ``set_state`` — both wrap
            shared-mutable state-holders on the tool instance per
            WP-1 audit §D).
          - All ``_pending_*`` queues + ``pending_injected_messages``
            start empty.

        The returned session is single-use; callers should drop the
        reference after the synthetic turn ends.
        """
        import copy

        forked_adapter = self.adapter
        fork_method = getattr(self.adapter, "fork", None)
        if callable(fork_method):
            forked_adapter = fork_method()

        forked_registry: ToolRegistry | None = None
        if self.registry is not None:
            forked_registry = ToolRegistry()
            for name, tool in self.registry.tools.items():
                if name in synthetic_excluded_tools:
                    continue
                forked_registry.register(tool)

        # Build the fresh ToolContext by shallow-copying the parent's
        # plumbing fields (ask_fn, status_emit, cli_sink — all shared by
        # intent) but giving the synthetic turn its own cancel_event /
        # todos / spawns.
        parent_ctx = self.tool_context
        synthetic_spawns = SpawnRegistry()
        forked_ctx = ToolContext(
            workspace_root=parent_ctx.workspace_root,
            session_id=parent_ctx.session_id,
            current_call_id="",
            posture_source="",
            cli_sink=parent_ctx.cli_sink,
            pty_dispatcher=parent_ctx.pty_dispatcher,
            scheduler_provider=parent_ctx.scheduler_provider,
            tool_registry_provider=parent_ctx.tool_registry_provider,
            # A synthetic turn delegates like any other, and delegation runs
            # on a lane — without these the fork would have no lane path.
            lane_manager_provider=parent_ctx.lane_manager_provider,
            named_lane_manager_provider=parent_ctx.named_lane_manager_provider,
            ask_fn=parent_ctx.ask_fn,
            status_emit=parent_ctx.status_emit,
            cancel_event=asyncio.Event(),
            todos=[],
            spawns=synthetic_spawns,
            # trio W3 — a synthetic fork is the SAME session identity running
            # a parallel turn, not a nested spawn: same depth, same cap.
            spawn_depth=parent_ctx.spawn_depth,
            spawn_depth_cap=parent_ctx.spawn_depth_cap,
        )

        forked = ChatSession(
            adapter=forked_adapter,
            system_prompt=self.system_prompt,
            max_tool_iterations=self.max_tool_iterations,
            max_consecutive_adapter_errors=self.max_consecutive_adapter_errors,
            options=self.options,
            history=copy.deepcopy(self.history),
            registry=forked_registry,
            tool_context=forked_ctx,
            compact_threshold=self.compact_threshold,
            keep_recent_turns=self.keep_recent_turns,
            head_anchor_messages=self.head_anchor_messages,
            active_window_tokens=self.active_window_tokens,
            summary_char_budget=self.summary_char_budget,
            ask_fn=self.ask_fn,
            policy=self.policy,
            prompt_builder=self.prompt_builder,
            session_kind=self.session_kind,
            channel_display_name=self.channel_display_name,
            cost_ledger=self.cost_ledger,
            overage_ask_fn=self.overage_ask_fn,
            spawns=synthetic_spawns,
            # M5 — a synthetic fork is the same session identity: carry the
            # concurrent-spawn cap too (the comment above promised "same cap"
            # but only depth was threaded; __post_init__ stamps it onto
            # synthetic_spawns).
            spawn_max_concurrent=self.spawn_max_concurrent,
        )
        return forked

    def attach_observer_subscriber(self, subscriber: Any) -> None:
        self._observer_subscriber = subscriber
        self._observer_last_index = len(self.history)

    def detach_observer_subscriber(self) -> None:
        self._observer_subscriber = None

    def ingest_memory_suggestion(self, suggestion: MemorySuggestion) -> bool:
        """Queue a suggestion for injection on the next turn.

        Returns `False` if `observation_id` was already seen (dedupe),
        `True` if newly accepted.
        """
        if suggestion.observation_id in self._observed_ids:
            return False
        self._observed_ids.append(suggestion.observation_id)
        self._pending_suggestions.append(suggestion)
        return True

    def ingest_conscience_transition(self, transition: dict[str, Any]) -> None:
        """Queue a synthetic `[conscience_drift]` note for next-turn injection.

        Same one-shot semantics as memory suggestions — surfaces as an
        extra user message visible on iteration 0 of the next turn, then
        drops (never persisted to `self.history`). Fired by
        `ConscienceHeartbeatJob` on worst-status band transition so the assistant
        feels drift instead of only being able to query it via
        `conscience_status`.
        """
        self._pending_conscience.append(_format_conscience_transition(transition))

    def ingest_spawn_completion(self, handle: Any) -> None:
        """Queue a finished background spawn for next-turn injection.

        Wired as ``SpawnRegistry.completion_notifier`` in ``__post_init__`` and
        fired once per spawn from its task done-callback. Same one-shot
        semantics as conscience notes: surfaces as an extra user message on
        iteration 0 of the next turn, then drops (never persisted to history).
        This is the fix for "the assistant forgets to check background spawns" — the
        completion now reaches the LLM rather than only the UI's SPAWN_DONE.

        The handle id is tracked alongside the note so the durable record
        `SpawnRegistry` wrote before calling here can be claimed once the note
        actually enters a turn.
        """
        record = record_from_handle(handle)
        self._pending_spawn_completions.append(_format_completion_record(record))
        if record.handle_id:
            self._queued_completion_ids.append(record.handle_id)

    def replay_undelivered_completions(self, chat_id: str) -> int:
        """Re-queue every recorded completion this chat was never actually told
        about, and return how many. Returns 0 for an unknown/empty chat.

        Called at restore alongside ``mark_vanished_spawns``, with the chat's
        OWN id (not the prior session's) — the store is chat-keyed precisely so
        a result outlives the session it was produced under. Anything already
        queued in this rebuilt session is skipped: the reconnect path folds a
        dead-window completion in by hand before this runs, and it must not
        arrive twice.
        """
        from tesseract.brain import completion_store

        try:
            outstanding = completion_store.pending(chat_id)
        except Exception:  # noqa: BLE001 — a restore never fails on this
            logger.warning(
                "completion replay failed for chat %s", chat_id, exc_info=True
            )
            return 0
        already = set(self._queued_completion_ids)
        replayed = 0
        for record in outstanding:
            if record.handle_id in already:
                continue
            self._pending_spawn_completions.append(_format_completion_record(record))
            self._queued_completion_ids.append(record.handle_id)
            replayed += 1
        return replayed

    def confirm_spawn_delivery(self) -> None:
        """Advance the delivery cursor for the completions this turn drained.

        Called by the Mirror turn runner once the turn's stream has completed —
        the point at which the model has actually read the block. Idempotent: a
        second call after the stash is empty is a no-op.
        """
        ids = self._delivering_completion_ids
        self._delivering_spawn_blocks = []
        self._delivering_completion_ids = []
        chat_id = self.spawns.chat_id
        if not ids or not chat_id:
            return
        from tesseract.brain import completion_store

        completion_store.mark_delivered(chat_id, ids)

    def rollback_spawn_delivery(self) -> None:
        """Put the drained notes back and leave the cursor where it was.

        Called when a turn ends without committing — cancelled, adapter error,
        the process taken down mid-stream. The notes go back to the FRONT of
        the queue: they are older than anything that landed while the turn was
        running, and the delivery order the operator sees should say so. The
        durable records were never claimed, so the same results also survive a
        restart that happens before the retry.
        """
        blocks = self._delivering_spawn_blocks
        ids = self._delivering_completion_ids
        self._delivering_spawn_blocks = []
        self._delivering_completion_ids = []
        if blocks:
            self._pending_spawn_completions.extendleft(reversed(blocks))
        if ids:
            self._queued_completion_ids[:0] = ids

    def ingest_spawn_stall(self, handle: Any) -> None:
        """Queue a one-shot `[spawn_stalled]` note for a wedged background spawn.

        Stage 2B. Driven from the per-turn sweep (`_sweep_stalled_spawns`); the
        registry's `sweep_stalled` dedups so each stalled handle fires once.
        Rides the same floor queue + one-shot iteration-0 drain as completions.
        """
        self._pending_spawn_completions.append(_format_spawn_stall(handle))

    def ingest_spawn_lost(
        self, handle_id: str, kind: str, was_parked: bool = False
    ) -> None:
        """Queue a one-shot `[spawn_lost]` note for a spawn orphaned by a
        backend restart. Rides the same floor queue as completions/stalls —
        see `mark_vanished_spawns`."""
        self._pending_spawn_completions.append(
            _format_spawn_lost(handle_id, kind, was_parked)
        )

    def mark_vanished_spawns(self, session_id: str) -> int:
        """P6 Task 3 §G5 — resume-time sweep for spawns orphaned by a backend
        restart.

        Called once per rebuilt `ChatSession` at restore — the bulk rebuild
        in `session.py::_restore_persisted_chats` (page reload / backend
        resume) and the archived-chat restore in `ws.py::_handle_chat_restore`
        — with the `ChatRecord.session_id` the chat was created under, NOT
        this fresh session's id: the vanished spawn's journal lives under the
        OLD identity. Each orphan (a `start` event with no matching
        `terminal` event) gets a one-shot `[spawn_lost]` note; the journal
        sweep itself marks them terminal so a second restore of the same
        record can't re-report them (idempotent by construction — no
        additional in-memory dedup needed here). Returns the orphan count
        (also folded into the digest's cumulative-since-boot failures
        counter via `failures_signal.record_vanished`).
        """
        from tesseract.brain import failures_signal, spawn_journal

        orphans = spawn_journal.sweep_orphans(session_id)
        for orphan in orphans:
            self.ingest_spawn_lost(
                orphan.get("handle_id", "?"),
                orphan.get("kind", "unknown"),
                was_parked=bool(orphan.get("was_parked")),
            )
        if orphans:
            failures_signal.record_vanished(len(orphans))
        return len(orphans)

    def _sweep_stalled_spawns(self) -> None:
        """Flag background spawns stuck `running` past the watchdog bound.

        No-op when the watchdog is disabled (`spawn_stall_seconds is None`).
        Called at turn start, before the injection drain, so a freshly-flagged
        stall surfaces in the same turn. Newly-flagged stalls also bump the
        digest's cumulative-since-boot failures counter (P6 Task 3 §G4).
        """
        if self.spawn_stall_seconds is None:
            return
        stalled = self.spawns.sweep_stalled(self.spawn_stall_seconds)
        for handle in stalled:
            self.ingest_spawn_stall(handle)
        if stalled:
            from tesseract.brain import failures_signal

            failures_signal.record_stall(len(stalled))

    def has_pending_spawn_completions(self) -> bool:
        """True if a finished spawn is queued but not yet drained into a turn.

        Read by the Mirror idle-wake driver to decide whether a completion that
        landed mid-wake (after iteration-0 drain) needs a follow-up wake turn.
        """
        return bool(self._pending_spawn_completions)

    def _adapter_message(self, message: dict[str, Any]) -> dict[str, Any]:
        # Strip Mirror-only sidecars before forwarding to the provider —
        # `_meta` carries the per-turn model + token-usage Mirror replays
        # on resume, `_mid_turn` flags entries written by
        # `_drain_user_injections` (Phase 2), and providers reject
        # unknown message fields.
        if (
            "timestamp" not in message
            and "_meta" not in message
            and "_mid_turn" not in message
        ):
            return message
        clean = dict(message)
        clean.pop("timestamp", None)
        clean.pop("_meta", None)
        clean.pop("_mid_turn", None)
        return clean

    def _current_system_prompt(self) -> str:
        if self.prompt_builder is None:
            return self.system_prompt
        # Whole-phase review fix (2026-07-06) — `prompt_builder` is a
        # zero-arg `Callable[[], str]` shared across many `ChatSession`s
        # (the cockpit builder is the same closure for every cockpit
        # session; see `mirror/server/session.py::_build_chat_session`),
        # so this session's scope can't be passed as a call argument
        # without changing that signature for every caller. Bind it via
        # contextvar instead — `assemble_system_prompt` reads it back
        # through `failures_signal.active_scope()` when no explicit
        # `failures_scope` was passed. asyncio contextvars are per-Task,
        # so concurrent `send()` calls in sibling tasks never see each
        # other's bound scope.
        from tesseract.brain import failures_signal
        token = failures_signal.bind_scope(self._failures_scope_id)
        try:
            try:
                built = self.prompt_builder()
            except Exception:
                logger.exception("prompt_builder raised — falling back to frozen system_prompt")
                return self.system_prompt
            if not built:
                logger.warning("prompt_builder returned empty — falling back to frozen system_prompt")
                return self.system_prompt
            return built
        finally:
            failures_signal.reset_scope(token)

    def _messages_for_turn(self) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = []
        prompt = self._current_system_prompt()
        if prompt:
            msgs.append({"role": "system", "content": prompt})
        # Anchor the injection immediately before the current user turn
        # (which is the last history entry at send() start — see send()'s
        # one-shot clear after iteration 0).
        if self._turn_injection and self.history:
            msgs.extend(self._adapter_message(m) for m in self.history[:-1])
            msgs.append({"role": "user", "content": self._turn_injection})
            msgs.append(self._adapter_message(self.history[-1]))
        else:
            msgs.extend(self._adapter_message(m) for m in self.history)
        return _trim_to_budget(msgs, char_limit=PROMPT_CHAR_BUDGET)

    def _drain_pending_suggestions(
        self,
        *,
        workspace_origin: dict[str, str] | None = None,
    ) -> str:
        """Pop queued observer suggestions + conscience notes (always) plus —
        only when ``workspace_origin`` is provided — the single workspace
        comment OR operator_post that this synthetic turn is replying to.

        ``workspace_origin`` shape: ``{"event_id": str, "comment_id": str}``.
        When ``comment_id == event_id`` the turn was fired for an
        ``operator_post`` and the post with that ``event_id`` is drained;
        otherwise the operator comment with that ``comment_id`` is drained.
        Codex audit 2026-05-07 M1: pin the drain to the queued payload so a
        successful reply on turn A no longer marks unrelated items B, C
        delivered before their own queued turns execute.

        Workspace comments are formatted as ``[workspace_comment_on_<event_id>]
        {body}``; operator_post events as ``[workspace_post_on_<event_id>]
        {title} — {body}``. Codex audit 2026-05-06 M3: workspace items are
        only drained for synthetic workspace turns — generic chat turns must
        not pull workspace traffic into the chat surface. M2: the IDs are
        stashed for caller-driven mark-on-success; nothing is marked
        delivered here.
        """
        workspace_comment_blocks: list[str] = []
        workspace_post_blocks: list[str] = []
        if workspace_origin:
            target_event_id = workspace_origin.get("event_id") or ""
            target_comment_id = workspace_origin.get("comment_id") or ""
            # Disambiguate post vs. comment by ID equality. _start_workspace_post_turn
            # sets comment_id == event_id since there is no real comment yet
            # (ws.py:709); _start_workspace_turn passes the actual cmt_* id
            # which differs from the parent event_id (ws.py:671).
            if target_comment_id and target_comment_id != target_event_id:
                workspace_comment_blocks, comment_ids = _drain_workspace_comments(
                    target_comment_id,
                )
                self._pending_workspace_comment_ids.extend(comment_ids)
            elif target_event_id:
                workspace_post_blocks, event_ids = _drain_operator_posts(
                    target_event_id,
                )
                self._pending_workspace_event_ids.extend(event_ids)
        if (
            not self._pending_suggestions
            and not self._pending_conscience
            and not self._pending_spawn_completions
            and not workspace_comment_blocks
            and not workspace_post_blocks
        ):
            return ""
        blocks: list[str] = []
        while self._pending_suggestions:
            blocks.append(format_for_injection(self._pending_suggestions.popleft()))
        while self._pending_conscience:
            blocks.append(self._pending_conscience.popleft())
        spawn_blocks: list[str] = []
        while self._pending_spawn_completions:
            spawn_blocks.append(self._pending_spawn_completions.popleft())
        if spawn_blocks:
            # A second drain with a batch still uncommitted means this session
            # is driven by something with no commit gate wired. Release the
            # earlier batch rather than accumulating it forever — but release
            # is not a claim: the durable ids are dropped WITHOUT marking them
            # delivered, so the record stays outstanding and a restart
            # redelivers. Confirming here would advance the cursor for a turn
            # that never committed, which is the one thing this must not do.
            if self._delivering_spawn_blocks or self._delivering_completion_ids:
                logger.warning(
                    "spawn completions drained twice without a commit — this "
                    "session's driver has no delivery gate; %d record(s) left "
                    "outstanding, recoverable only by a restart (nothing in a "
                    "running session re-queues a released batch)",
                    len(self._delivering_completion_ids),
                )
                self._delivering_spawn_blocks = []
                self._delivering_completion_ids = []
            self._delivering_spawn_blocks = list(spawn_blocks)
            self._delivering_completion_ids = self._queued_completion_ids
            self._queued_completion_ids = []
        blocks.extend(_fit_spawn_completions(spawn_blocks))
        blocks.extend(workspace_comment_blocks)
        blocks.extend(workspace_post_blocks)
        return "\n\n".join(blocks)

    def confirm_workspace_delivery(self) -> None:
        """Mark all workspace comments / operator_posts drained on the
        current turn as delivered, then clear the stash.

        Called by the Mirror ws layer after the synthetic turn's
        ``workspace_reply`` tool result lands successfully. Idempotent —
        a second call after the stash is empty is a no-op (Codex M2).
        """
        comment_ids = self._pending_workspace_comment_ids
        event_ids = self._pending_workspace_event_ids
        self._pending_workspace_comment_ids = []
        self._pending_workspace_event_ids = []
        _mark_workspace_delivered(comment_ids, event_ids)

    def rollback_workspace_delivery(self) -> None:
        """Drop the stash without marking anything delivered.

        Called when a synthetic turn ends without a successful
        ``workspace_reply`` (cancel, adapter error, model ignored the
        directive). The next drain will re-include the same items so
        the operator's intent survives the failed attempt (Codex M2).
        """
        self._pending_workspace_comment_ids = []
        self._pending_workspace_event_ids = []

    def _tool_schemas(self) -> list[dict[str, Any]] | None:
        if self.registry is None or not self.registry.tools:
            return None
        return self.registry.schemas_for_adapter(
            enabled_extended=self._enabled_extended_tools
        )

    async def send(
        self,
        user_text: str | list[dict[str, Any]],
        *,
        transient: bool = False,
        workspace_origin: dict[str, str] | None = None,
        view_snapshot: dict[str, Any] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        """Append a user turn; run the tool-call loop; yield every chunk.

        On `asyncio.CancelledError` (operator Ctrl+C), persists any partial
        assistant text as a plain terminated turn (tool calls mid-stream are
        dropped so history stays consistent) and re-raises.

        ``transient=True`` (workspace synthetic turn): the user message and
        any assistant output are NOT persisted to ``self.history``, so the
        synthetic turn does not pollute the chat conversation. Drain still
        runs (operator's workspace comments are delivered) and tools still
        execute (the assistant calls workspace_reply). The caller is responsible for
        suppressing chat-text envelopes downstream.

        ``workspace_origin`` (dict | None) opts the turn into the workspace
        drain path (Codex audit 2026-05-06 M3). The dict carries
        ``{"event_id", "comment_id"}`` of the queued payload that triggered
        this synthetic turn — the drain pulls only that specific item from
        the store so a successful reply on turn A cannot mark unrelated
        items delivered before their own turns run (Codex audit 2026-05-07
        M1). ``None`` keeps the workspace stores out of the chat turn.
        """
        # Preflight cap check — fires once per send() before mutating state so
        # a blocked turn leaves history and cancel_event untouched. Only
        # tier:api roles are gated; tier:cli (Claude/Codex subscriptions) and
        # the no-tier default pass through.
        if self.cost_ledger is not None and (self.options.tier or "api") == "api":
            role = self.options.role or "chat_brain"
            try:
                self.cost_ledger.check_preflight(role)
            except BudgetExhausted as exc:
                # Cost UX overhaul: ask the operator before aborting.
                # On approve, unlock the scope for the rest of the day
                # and re-enter preflight (which now passes). On deny —
                # or no callback — fall through to the original error
                # chunk path so the turn still aborts cleanly.
                approved = False
                # A paused source (operator budget.pause_source) is an explicit
                # hold, NOT a cap overage — the overage-ask card would mislead and
                # its unlock can't lift a pause anyway. Fall straight to the
                # informative error chunk. Cap overages keep the ask-to-continue UX.
                if self.overage_ask_fn is not None and exc.scope != "paused":
                    try:
                        approved = await self.overage_ask_fn(exc)
                    except Exception:
                        approved = False
                if approved:
                    self.cost_ledger.unlock_overage(exc.scope_key())
                    # Re-check preflight; a *different* scope (e.g. global
                    # while we unlocked role) may still block. If it does,
                    # raise to the original abort path below.
                    try:
                        self.cost_ledger.check_preflight(role)
                    except BudgetExhausted as exc2:
                        exc = exc2
                        approved = False
                if not approved:
                    yield StreamChunk(
                        type=ChunkType.ERROR,
                        error=str(exc),
                        raw={
                            "severity": "warning",
                            "reason": "budget_exhausted",
                            "scope": exc.scope,
                            "role": exc.role,
                            "spent_usd": exc.spent_usd,
                            "cap_usd": exc.cap_usd,
                        },
                    )
                    return

        self.tool_context.cancel_event.clear()
        # P6 Task 5 — a new turn starts a fresh consecutive-error count
        # (streak is "within a turn"); the visible failures_signal line
        # from a PRIOR turn's streak is left alone — it only clears when
        # the flagged tool actually succeeds.
        self._tool_error_streak_name = ""
        self._tool_error_streak_count = 0
        self.history.append({
            "role": "user",
            "content": user_text,
            "timestamp": _now_iso(),
        })
        appended_user_idx = len(self.history) - 1
        # Stage 2B halt-watchdog — flag wedged background spawns BEFORE the
        # drain so a fresh `[spawn_stalled]` note rides this turn's injection.
        self._sweep_stalled_spawns()
        self._turn_injection = self._drain_pending_suggestions(
            workspace_origin=workspace_origin,
        )
        # Synthetic workspace turns: signal post-send cleanup to drop the
        # appended user turn + any assistant turns that follow it. Tracked
        # here, applied in the `finally` block.
        transient_start_idx = appended_user_idx if transient else -1

        # Auto memory recall (lean-agent-os P1 Task 3) — replaces the old
        # regex-based recall-intent nudge. Every operator turn is embedded
        # and retrieved via the SAME pipeline `memory_search` uses (see
        # `auto_recall.py`); relevant hits are injected as a
        # `[recalled_memories]` block. Same one-turn lifetime as observer
        # suggestions — cleared inside `_messages_for_turn`. Best-effort:
        # any retrieval failure (embedder down, etc.) yields no block and
        # the turn proceeds untouched.
        memory_tool = self.registry.get("memory_search") if self.registry is not None else None
        pipeline = getattr(memory_tool, "pipeline", None)
        if pipeline is not None:
            recall_cfg = load_auto_recall_config()
            recall_items = await auto_recall(
                _flatten_message_text(user_text),
                pipeline,
                top_k=recall_cfg.top_k,
                char_cap=recall_cfg.char_cap,
                min_similarity=recall_cfg.min_similarity,
                min_query_words=recall_cfg.min_query_words,
                exclude_ids=self._recall_excluded_ids(),
            )
            self._record_recall_injection(
                {it.memory_id for it in recall_items}, recall_cfg.dedup_window_turns
            )
            recall_block = format_recall_block(recall_items)
            if recall_block:
                self._turn_injection = (
                    f"{recall_block}\n\n{self._turn_injection}"
                    if self._turn_injection
                    else recall_block
                )

        # MP-2 ambient observer: prepend `current_view` + `view_state`
        # so chat brain can resolve "help me with this row" against the
        # operator's current Mirror tab. Same one-turn lifetime — the
        # injection is cleared post-iteration-0 by `_messages_for_turn`.
        if view_snapshot:
            block = _format_view_snapshot(view_snapshot)
            if block:
                self._turn_injection = (
                    f"{block}\n\n{self._turn_injection}"
                    if self._turn_injection
                    else block
                )

        try:
            if self.options.provider and self.options.model:
                yield StreamChunk(
                    type=ChunkType.MODEL_SELECTED,
                    raw={
                        "role": self.options.role or "chat_brain",
                        "provider": self.options.provider,
                        "model": self.options.model,
                        "tier": self.options.tier or "api",
                        "reasoning_effort": self.options.reasoning_effort or "",
                    },
                )

            # Seed from the pre-loop MODEL_SELECTED so the very first iteration
            # has model identity even if the adapter chain doesn't re-emit one.
            # FallbackAdapter overwrites mid-stream when the actual entry differs.
            seeded_model_meta: dict[str, str] | None = None
            if self.options.provider and self.options.model:
                seeded_model_meta = {
                    "role": self.options.role or "chat_brain",
                    "provider": self.options.provider,
                    "model": self.options.model,
                    "tier": self.options.tier or "api",
                }

            iteration = 0
            cap_resets = 0
            # Total tool invocations across the whole turn. Used by the
            # chat-turn promise audit at terminal `return` to detect
            # claim-without-action confabulation (the 2026-05-19 Telegram
            # incident: model said "Done. Every 15 min I'll fire a toast"
            # without invoking schedule_update).
            turn_tool_invocations = 0
            # One re-entry per turn for a terminal message that says nothing
            # (status-only) or was cut off. Deliberately a single budget shared
            # by both shapes — the guard exists to break silence, not to argue
            # with a model that keeps failing the contract.
            terminal_retry_used = False
            while True:
                if iteration >= self.max_tool_iterations:
                    # Operator policy: do NOT break the turn at the cap.
                    # Surface a soft notice so the UI shows the reset
                    # (orb stays calm, no red bubble), then zero the
                    # counter and keep streaming. Daily cost caps and
                    # the consecutive-adapter-error breaker remain the
                    # real safety net against runaway spend.
                    cap_resets += 1
                    yield StreamChunk(
                        type=ChunkType.ERROR,
                        error=(
                            f"Tool-loop cap ({self.max_tool_iterations}) hit — "
                            f"resetting and continuing (reset #{cap_resets})."
                        ),
                        raw={
                            "severity": "soft",
                            "reason": "tool_cap_reset",
                            "resets": cap_resets,
                        },
                    )
                    logger.warning(
                        "tool-loop iteration cap hit (%d) — resetting and continuing (reset #%d)",
                        self.max_tool_iterations, cap_resets,
                    )
                    iteration = 0
                assistant_text: list[str] = []
                pending_calls: list[ToolCall] = []
                reasoning_items: list[dict[str, Any]] = []
                stop_reason = ""
                last_usage: dict[str, int] | None = None
                last_model_meta: dict[str, str] | None = seeded_model_meta
                adapter_error_seen = False
                adapter_error_text = ""
                adapter_error_soft = False

                # Timed on the first iteration only. Prompt assembly is the one
                # piece of the "brain" leg that is ours rather than the
                # provider's, and without a number for it a slow turn can only
                # be blamed on the API rather than shown to be its fault.
                _assembly_started = time.monotonic() if iteration == 0 else 0.0
                messages = self._messages_for_turn()
                if iteration == 0:
                    _assembly_took = time.monotonic() - _assembly_started
                    # INFO only when it is worth a line. Every turn passes
                    # through here — typed, channel, synthetic, background —
                    # and a 0.02 s assembly is noise in a log that gets read
                    # during incidents. The number matters when it is large.
                    logger.log(
                        logging.INFO if _assembly_took >= _SLOW_PROMPT_ASSEMBLY_S
                        else logging.DEBUG,
                        "chat: prompt assembled in %.2fs", _assembly_took,
                    )
                # One-shot: injection visible on iteration 0 only — on re-
                # entry `self.history[-1]` is no longer the user turn, so
                # anchoring would mis-place the suggestion.
                self._turn_injection = ""
                # Wrap-up nudge: on the final iteration of each tool-loop
                # cycle, hint the model to stop calling tools and synthesize.
                # Guarded against `cap == 1` (degenerate test config where
                # iteration 0 is also `cap - 1` — the nudge would fire on
                # the very first call and the model would never get to use
                # any tool).
                if (
                    self.max_tool_iterations > 1
                    and iteration == self.max_tool_iterations - 1
                ):
                    messages.append({
                        "role": "system",
                        "content": (
                            "Final iteration of this turn — you will not be able to call "
                            "more tools. Do NOT call any tool now. Synthesize your best "
                            "answer from what you have found so far and respond in plain text."
                        ),
                    })
                try:
                    async for chunk in self.adapter.stream(
                        messages=messages,
                        tools=self._tool_schemas(),
                        options=self.options,
                    ):
                        if chunk.type == ChunkType.TEXT:
                            assistant_text.append(chunk.text)
                        elif chunk.type == ChunkType.TOOL_CALL_END:
                            if chunk.tool_call is not None:
                                pending_calls.append(chunk.tool_call)
                        elif chunk.type == ChunkType.REASONING_ITEM:
                            item = chunk.raw.get("item") if chunk.raw else None
                            if isinstance(item, dict):
                                reasoning_items.append(item)
                            continue  # adapter-internal — never forward to UI
                        elif chunk.type == ChunkType.MODEL_SELECTED:
                            raw = chunk.raw or {}
                            last_model_meta = {
                                "role": str(raw.get("role") or self.options.role or "chat_brain"),
                                "provider": str(raw.get("provider") or ""),
                                "model": str(raw.get("model") or ""),
                                "tier": str(raw.get("tier") or self.options.tier or "api"),
                            }
                        elif chunk.type == ChunkType.STOP:
                            stop_reason = chunk.stop_reason
                            usage_raw = (chunk.raw or {}).get("usage") if isinstance(chunk.raw, dict) else None
                            if isinstance(usage_raw, dict):
                                last_usage = {
                                    "input_tokens": int(usage_raw.get("input_tokens") or 0),
                                    "output_tokens": int(usage_raw.get("output_tokens") or 0),
                                    "cached_tokens": int(usage_raw.get("cached_tokens") or 0),
                                }
                            self._record_turn_usage(chunk)
                        elif chunk.type == ChunkType.ERROR:
                            # Adapter-side error (chain exhausted, post-
                            # commit 5xx, etc.). Yield to the UI for the
                            # red bubble, then drop out of the inner stream
                            # so the outer loop can decide whether to
                            # retry or give up. The previous behaviour
                            # (`yield chunk; return`) left the assistant unable to
                            # see or recover from the error — Layer 2 fix
                            # 2026-05-05.
                            yield chunk
                            severity_raw = (
                                (chunk.raw or {}).get("severity")
                                if isinstance(chunk.raw, dict) else None
                            )
                            if severity_raw == "warning":
                                # Cost-cap, budget-exhausted, tool-cap
                                # warnings — these are intentional aborts
                                # from elsewhere, not adapter failures.
                                # Preserve the existing terminate-cleanly
                                # contract.
                                return
                            adapter_error_seen = True
                            adapter_error_text = chunk.error or "unknown"
                            # Soft = post-commit provider hiccup (chain
                            # already advanced or recovery owed to the
                            # next iteration). The retry still runs, but
                            # don't bump the chat_brain breaker — these
                            # are streaming-layer flakes, not the kind of
                            # consecutive hard failures the breaker exists
                            # to gate.
                            adapter_error_soft = severity_raw == "soft"
                            break
                        yield chunk
                except asyncio.CancelledError:
                    if assistant_text:
                        assistant_text.append("\n\n[interrupted by operator]")
                        self._append_assistant_message(assistant_text, [], last_model_meta, last_usage)
                    raise

                if adapter_error_seen:
                    # Persist any partial assistant text so the chat log
                    # shows what the assistant started saying before the adapter
                    # died. Pending tool calls are dropped — they were
                    # never executed and re-emitting them on the retry
                    # would mismatch tool_call_ids.
                    if assistant_text:
                        assistant_text.append("\n\n[interrupted by adapter error]")
                        self._append_assistant_message(assistant_text, [], last_model_meta, last_usage)
                    if not adapter_error_soft:
                        # Soft = post-commit provider hiccup; the chain
                        # already disclosed it via the soft envelope and
                        # the next iteration will recover. Skip the
                        # breaker bump so a flaky stream doesn't trip the
                        # chat_brain circuit-breaker on its own. The
                        # counter is "consecutive across turns" (only
                        # reset on a successful STOP at line 580), so
                        # hard errors still trip at exactly the cap.
                        self._consecutive_adapter_errors += 1
                    if self._consecutive_adapter_errors >= self.max_consecutive_adapter_errors:
                        logger.warning(
                            "chat_brain circuit-breaker tripped after %d consecutive adapter errors",
                            self._consecutive_adapter_errors,
                        )
                        yield StreamChunk(
                            type=ChunkType.ERROR,
                            error=(
                                f"chat_brain circuit-breaker tripped — "
                                f"{self._consecutive_adapter_errors} consecutive adapter errors. "
                                f"Last: {adapter_error_text}"
                            ),
                            raw={
                                "severity": "warning",
                                "reason": "consecutive_adapter_errors",
                                "consecutive": self._consecutive_adapter_errors,
                            },
                        )
                        # Reset so the next send() starts clean — the
                        # operator will likely send a follow-up turn.
                        self._consecutive_adapter_errors = 0
                        return
                    # Inject a synthetic system message so the assistant sees the
                    # error on the next iteration and can self-correct
                    # (Layer 2 + Layer 3 directive in prompt.py).
                    self.history.append({
                        "role": "system",
                        "content": (
                            f"[chat_brain error] {adapter_error_text}. "
                            f"The runtime is retrying this turn. "
                            f"Inspect the cause: if it looks like your mistake "
                            f"(bad path, malformed args, wrong tool), call "
                            f"memory_save once with a one-line feedback note so "
                            f"you don't repeat it. If it looks external (5xx, "
                            f"network, rate-limit), no memory action is needed — "
                            f"the runtime already retried. Then re-attempt the "
                            f"original goal once."
                        ),
                        "timestamp": _now_iso(),
                    })
                    iteration += 1
                    continue

                # Successful adapter stream — reset the breaker counter.
                self._consecutive_adapter_errors = 0

                # Reasoning items must precede the assistant message they explain.
                for item in reasoning_items:
                    self.history.append({"_reasoning": True, **item})
                self._append_assistant_message(assistant_text, pending_calls, last_model_meta, last_usage)

                if not pending_calls:
                    # A turn may not end mutely. Two shapes reach this branch
                    # looking like a finished turn and are not one: a message
                    # that is only `<intent>` (status, never rendered as a
                    # reply) and a message the provider cut off. Both get one
                    # re-entry with a system nudge; the iteration cap and the
                    # adapter breaker stay the outer net.
                    # Channels are exempt from the status-only half. A channel
                    # has no tag contract, and `_extract_channel_reply` ends on
                    # a tier that deliberately strips `<intent>` and sends the
                    # sentence anyway — so on Telegram that message reached the
                    # operator and the turn was never silent. Retrying it would
                    # buy a wasted roundtrip and risk a doubled reply, since the
                    # extractor runs over the whole concatenated text.
                    # Truncation is not exempt: a cut-off stream is a defect on
                    # every surface.
                    status_only = (
                        self.session_kind != "channel"
                        and not _has_operator_visible_text(assistant_text)
                    )
                    truncated = _is_truncated_stop(stop_reason)
                    if (status_only or truncated) and not terminal_retry_used:
                        terminal_retry_used = True
                        if status_only:
                            logger.warning(
                                "chat: status-only terminal message (stop=%s iter=%d) — "
                                "retrying once before the turn is allowed to end",
                                stop_reason, iteration,
                            )
                            # Worded to stay valid even when the re-entry lands
                            # on the wrap-up iteration, which forbids tools:
                            # answering is always the instruction, calling a
                            # tool is the conditional.
                            nudge = (
                                "[runtime] Your last message carried no reply — it was a "
                                "status line with no tool calls, so the operator saw an "
                                "empty bubble. Answer now inside <answer>...</answer>, or "
                                "call the tool you announced if you are still allowed to. "
                                "Do not send another status-only message."
                            )
                        else:
                            logger.warning(
                                "chat: truncated stop %r on a tool-less message (iter=%d) — "
                                "retrying once; the reply was cut off, not finished",
                                stop_reason, iteration,
                            )
                            nudge = (
                                f"[runtime] Your last response was cut off "
                                f"({stop_reason}) before it finished. Continue from where "
                                f"it stopped and keep it short enough to complete. If you "
                                f"were about to call a tool, call it if you are still "
                                f"allowed to."
                            )
                        self.history.append({
                            "role": "system",
                            "content": nudge,
                            "timestamp": _now_iso(),
                        })
                        iteration += 1
                        continue

                    if status_only:
                        # The retry came back status-only too. Say so — a
                        # silent stop is the worst available outcome, and the
                        # operator waited eight minutes on one before quitting
                        # the app. Soft severity: the turn is over, but this is
                        # a contract failure, not an adapter fault.
                        logger.error(
                            "chat: turn ended with no reply — status-only message twice "
                            "in a row (stop=%s iter=%d tools=%d)",
                            stop_reason, iteration, turn_tool_invocations,
                        )
                        yield StreamChunk(
                            type=ChunkType.ERROR,
                            error=(
                                "The turn ended without a reply — the assistant sent a "
                                "status line with no tool calls twice in a row. Nothing "
                                "was said. Ask again, or rephrase."
                            ),
                            raw={
                                "severity": "soft",
                                "reason": "status_only_turn",
                                "stop_reason": stop_reason,
                            },
                        )
                    elif truncated:
                        logger.warning(
                            "chat: turn ended on a truncated stop %r after one retry "
                            "(iter=%d) — the reply the operator sees is incomplete",
                            stop_reason, iteration,
                        )
                    # Codex audit-2 follow-up: chat-turn promise audit.
                    # If the final assistant text contains action-claim
                    # language AND no tool was invoked across the whole
                    # turn, surface a WARNING so the operator can spot
                    # confabulation. Logging-only for now — the 2026-05-19
                    # nudge incident motivated this; if the model bump
                    # alone isn't enough we can layer in a self-correction
                    # step here later.
                    _audit_promise_without_action(
                        assistant_text=assistant_text,
                        turn_tool_invocations=turn_tool_invocations,
                        options=self.options,
                        status_only_terminal=status_only,
                    )
                    logger.debug(
                        "turn complete: iter=%d stop=%s chars=%d tools=%d",
                        iteration, stop_reason,
                        sum(len(p) for p in assistant_text),
                        turn_tool_invocations,
                    )
                    return

                turn_tool_invocations += len(pending_calls)
                async for result_chunk in self._run_pending_calls(pending_calls):
                    yield result_chunk

                injected = self._drain_user_injections()
                if injected:
                    yield StreamChunk(
                        type=ChunkType.USER_INJECT,
                        raw={"injected": injected, "count": len(injected)},
                    )

                # Phase 4: surface any background spawns that completed
                # between iterations. The chunk carries handle id +
                # kind + status + summary so the frontend can clear
                # the "running" badge on the corresponding DelegateCard
                # and the assistant sees the completion in chat (it can then
                # call spawn_await if it actually needs the output).
                for handle in self.spawns.drain_completed():
                    summary = _summarize_spawn(handle)
                    yield StreamChunk(
                        type=ChunkType.SPAWN_DONE,
                        raw={
                            "handle": handle.handle_id,
                            "kind": handle.kind,
                            "status": handle.status(),
                            "started_at": handle.started_at,
                            "finished_at": handle.finished_at,
                            "summary": summary,
                        },
                    )

                iteration += 1
        finally:
            # Observer notify fires here so every exit path reaches it:
            # normal completion, tool-cap overflow, operator cancel. The
            # method is idempotent (no-op when the watermark equals history
            # length), so a single call in finally covers all paths.
            #
            # Transient (workspace synthetic) turns: skip the observer notify
            # entirely. The synthetic user/assistant entries are about to be
            # rolled back below, so notifying the observer would (a) feed it
            # a fabricated turn that will be erased and (b) advance
            # `_observer_last_index` past the post-rollback end of history,
            # causing the next real turn's slice to come up empty.
            if not transient:
                try:
                    self._notify_observer_turn_end()
                except Exception:
                    logger.exception("observer notify in finally raised")
                # AU-16 leaf stream — emit one MemoryLeaf per completed
                # turn so the per-source / per-topic / global trees fill
                # with real activity. Synthetic workspace turns are
                # excluded (same reason as observer). Failure logs and
                # drops; the chat turn must never break on a leaf-write
                # issue.
                try:
                    self._emit_turn_leaf(appended_user_idx)
                except Exception:
                    logger.exception("leaf emit in finally raised")
            self._strip_attachment_data_from_history(appended_user_idx)
            self._turn_injection = ""
            # Synthetic workspace turn cleanup: drop the synthetic user
            # turn AND any assistant turns appended after it. Keeps chat
            # history free of `[workspace_origin]` framing so subsequent
            # operator chat turns aren't biased by it.
            if transient and 0 <= transient_start_idx < len(self.history):
                del self.history[transient_start_idx:]

    def _record_turn_usage(self, stop_chunk: StreamChunk) -> None:
        """Extract token counts from the STOP chunk and bill the ledger.

        Called once per adapter.stream() STOP — i.e. per tool-loop iteration.
        Each iteration is a separate billable roundtrip to the provider, so
        the ledger sees one event per iteration; `record()`'s internal daily
        accumulator stitches them into the per-role daily total.

        When the adapter is a FallbackAdapter wrapper, the entry that
        actually streamed may differ from `self.options` (failover). Read
        `last_used_options` if exposed so failover spend is billed to the
        right model name in the per-role daily total (W3 reviewer
        follow-up, 2026-04-29).
        """
        if self.cost_ledger is None:
            return
        billed_options = getattr(self.adapter, "last_used_options", None) or self.options
        if not billed_options.model:
            return
        raw = stop_chunk.raw or {}
        usage = raw.get("usage") if isinstance(raw, dict) else None
        if not isinstance(usage, dict):
            return
        try:
            self.cost_ledger.record(
                billed_options.role or "chat_brain",
                billed_options.model,
                CostUsage(
                    input_tokens=int(usage.get("input_tokens") or 0),
                    output_tokens=int(usage.get("output_tokens") or 0),
                    cached_tokens=int(usage.get("cached_tokens") or 0),
                    cache_creation_tokens=int(usage.get("cache_creation_tokens") or 0),
                ),
            )
        except RuntimeError:
            # Missing pricing entry in roles.yaml — logged inside ledger; the
            # turn itself must not die because the accountant can't price it.
            logger.exception("cost ledger record failed")

    def _notify_observer_turn_end(self) -> None:
        sub = self._observer_subscriber
        if sub is None or not getattr(sub, "is_active", False):
            return
        start = self._observer_last_index
        self._observer_last_index = len(self.history)
        new_turns = [
            {"role": m["role"], "content": m["content"]}
            for m in self.history[start:]
            if m.get("role") in ("user", "assistant")
            and isinstance(m.get("content"), str)
            and m["content"].strip()
        ]
        if not new_turns:
            return
        try:
            sub.on_loop_end(new_turns)
        except Exception:
            logger.exception("observer subscriber on_loop_end raised")

    def _append_assistant_message(
        self,
        assistant_text: list[str],
        pending_calls: list[ToolCall],
        model_meta: dict[str, str] | None = None,
        usage: dict[str, int] | None = None,
    ) -> None:
        if not assistant_text and not pending_calls:
            return
        msg: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(assistant_text),
            "timestamp": _now_iso(),
        }
        if pending_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.input),
                    },
                    # Carried only when the provider issued one, so the shape
                    # is unchanged for every other provider and for history
                    # written before this existed. It has to live on the
                    # message rather than in the adapter, because the adapter
                    # instance is rebuilt on a config change and the history
                    # outlives the process — a resumed session replays these
                    # calls, and Gemini 3 rejects the request if the signature
                    # it issued does not come back with them.
                    **(
                        {"provider_signature": tc.provider_signature}
                        if tc.provider_signature
                        else {}
                    ),
                }
                for tc in pending_calls
            ]
        # `_meta` is rehydrated by the Mirror so resumed bubbles show the same
        # model badge and `cache xxx in / yyy cached` pill they had live. The
        # underscore prefix matches `_reasoning` — model adapters MUST ignore
        # any key starting with `_` when forwarding history to the provider.
        meta: dict[str, Any] = {}
        if model_meta and model_meta.get("provider") and model_meta.get("model"):
            meta["model"] = dict(model_meta)
        if usage and (usage.get("input_tokens") or usage.get("output_tokens") or usage.get("cached_tokens")):
            meta["usage"] = dict(usage)
        if meta:
            msg["_meta"] = meta
        self.history.append(msg)

    def _emit_turn_leaf(self, appended_user_idx: int) -> None:
        """Push one ``MemoryLeaf`` representing this completed turn into
        the AU-16 leaf stream. Source is derived from ``session_kind``
        (channel / cockpit). The lifecycle jobs (``ExtractChunkJob`` …
        ``DigestDailyJob``) pick it up on their next tick.

        Skips empty turns (no assistant response) — the leaf-extract
        floor would drop them anyway, no point allocating disk.
        """
        if appended_user_idx < 0 or appended_user_idx >= len(self.history):
            return
        user_msg = self.history[appended_user_idx]
        user_text = _flatten_message_text(user_msg.get("content"))
        assistant_text = _collect_assistant_text(
            self.history[appended_user_idx + 1:]
        )
        if not assistant_text.strip():
            return
        body_parts: list[str] = []
        if user_text.strip():
            body_parts.append(f"User: {user_text.strip()}")
        body_parts.append(f"Assistant: {assistant_text.strip()}")
        body = "\n\n".join(body_parts)
        title = (user_text.strip().splitlines() or [""])[0].strip()[:200]

        # Source: channel sessions get the channel name; cockpit sessions
        # fold into a single chat:cockpit slug for now. A future ChatSession
        # session_id field would split per-window.
        if self.session_kind == "channel" and self.channel_display_name:
            source = f"channel:{self.channel_display_name}"
        else:
            source = "chat:cockpit"

        from datetime import datetime, timezone
        from tesseract.memory.leaves import LeafState, LeafStore, MemoryLeaf, mint_leaf_id

        now = datetime.now(timezone.utc)
        leaf = MemoryLeaf(
            id=mint_leaf_id(),
            source=source[:200],
            created_at=now,
            updated_at=now,
            state=LeafState.PENDING_EXTRACTION,
            title=title or "(chat turn)",
            body=body[:20000],
        )
        LeafStore().add(leaf)

    def _strip_attachment_data_from_history(self, index: int) -> None:
        if index < 0 or index >= len(self.history):
            return
        content = self.history[index].get("content")
        if not isinstance(content, list):
            return
        cleaned: list[dict[str, Any]] = []
        changed = False
        for part in content:
            if not isinstance(part, dict):
                continue
            if "data" in part:
                part = {k: v for k, v in part.items() if k != "data"}
                changed = True
            cleaned.append(part)
        if changed:
            self.history[index]["content"] = cleaned

    async def _run_pending_calls(
        self,
        pending_calls: list[ToolCall],
    ) -> AsyncGenerator[StreamChunk, None]:
        if self.registry is None:
            for tc in pending_calls:
                msg = f"no tool registry; cannot execute {tc.name}"
                self.history.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": msg,
                    "timestamp": _now_iso(),
                })
                yield StreamChunk(
                    type=ChunkType.TOOL_RESULT,
                    text=msg,
                    tool_call_id=tc.id,
                )
            return

        # Partition by concurrency safety (audit M5, 2026-04-29). Tools
        # that mutate shared state — file_write, bash_tool, invoke_agent,
        # agent_create, delegate_*, schedule_*, set_mood/voice/state,
        # alarm_* — declare `is_concurrency_safe() is False`. Running
        # them via `asyncio.create_task` regardless raced two writes
        # against the same shared resource. Read-only tools
        # (memory_search, web_search, vault_search, file_read, glob,
        # grep, context7, etc.) stay parallel for latency.
        #
        # Each task gets its OWN ToolContext clone so the mutable
        # `current_call_id` field doesn't race across tasks
        # (`delegate_coder` / `delegate_auditor` and `ask_fn` read it
        # mid-execution). `dataclasses.replace` is a shallow copy —
        # cli_sink / pty_dispatcher / ask_fn references are preserved.
        #
        # Streaming: safe results stream as they finish (operator sees
        # fast tools land first, dead-air narrowed). Unsafe results
        # stream as each serial step completes. History append is
        # always in pending_calls index order so the next adapter call
        # sees a stable message sequence.
        async def _run_one(idx: int, tc: ToolCall) -> tuple[int, ToolCall, ToolResult]:
            per_call_ctx = dataclasses.replace(
                self.tool_context, current_call_id=tc.id,
            )
            result = await execute_tool(
                registry=self.registry,
                tool_name=tc.name,
                tool_input=tc.input,
                context=per_call_ctx,
                ask_fn=self.ask_fn,
                policy=self.policy,
            )
            return idx, tc, result

        safe_indices: list[int] = []
        unsafe_indices: list[int] = []
        for i, tc in enumerate(pending_calls):
            tool = self.registry.get(tc.name)
            if tool is not None and tool.is_concurrency_safe():
                safe_indices.append(i)
            else:
                unsafe_indices.append(i)

        # trio W4 — inherit a background spawn's `spawn:<handle_id>` name
        # prefix onto fan-out tool tasks: the Mirror ask_fn detects spawn
        # origin from the CURRENT task name (ask_gate.py::
        # _spawn_handle_id_of_current_task), and a concurrency-safe ASK tool
        # would otherwise hop to a `tool:*` task and hard-deny instead of
        # parking.
        _parent = asyncio.current_task()
        _parent_name = _parent.get_name() if _parent is not None else ""
        _spawn_prefix = (
            _parent_name.split("|", 1)[0] + "|"
            if _parent_name.startswith("spawn:")
            else ""
        )
        safe_tasks: list[asyncio.Task[tuple[int, ToolCall, ToolResult]]] = [
            asyncio.create_task(
                _run_one(i, pending_calls[i]),
                name=f"{_spawn_prefix}tool:{pending_calls[i].name}:{pending_calls[i].id}",
            )
            for i in safe_indices
        ]
        results: dict[int, tuple[ToolCall, ToolResult]] = {}

        def _result_chunk(tc: ToolCall, result: ToolResult) -> StreamChunk:
            raw: dict[str, Any] = {}
            if result.denied_hard:
                raw["denied_hard"] = True
                raw["deny_reason"] = result.deny_reason
                raw["tool_name"] = tc.name
            if result.metadata:
                raw["metadata"] = dict(result.metadata)
            return StreamChunk(
                type=ChunkType.TOOL_RESULT,
                text=result.output,
                tool_call_id=tc.id,
                error=result.output if result.is_error else "",
                raw=raw,
            )

        # Track which pending_call indices already have a `role: tool`
        # message in history. The success path (post-loop) writes them
        # in pending_calls order; the safety net in `finally` only fills
        # gaps. Without this set, an exception that fires AFTER the
        # success post-loop has partially written would double-append.
        history_written: set[int] = set()
        cancel_error: BaseException | None = None
        run_error: BaseException | None = None

        try:
            for completed in asyncio.as_completed(safe_tasks):
                idx, tc, result = await completed
                results[idx] = (tc, result)
                yield _result_chunk(tc, result)

            for i in unsafe_indices:
                idx, tc, result = await _run_one(i, pending_calls[i])
                results[idx] = (tc, result)
                yield _result_chunk(tc, result)

            # All tasks completed. Append history in pending_calls order so
            # the next adapter call sees a deterministic sequence regardless
            # of which tool finished first.
            for i, tc in enumerate(pending_calls):
                tc_done, result = results[i]
                # Audit-3 M9 — wrap untrusted tool output (file/web/vault
                # bodies) in the UNTRUSTED_TOOL_OUTPUT envelope before
                # the model history sees it. Without this, a markdown
                # file or web snippet containing ``<system-reminder>``
                # or "ignore previous instructions" reaches the model
                # as raw text and may be obeyed. The envelope is
                # idempotent so a tool that already wraps its own
                # output won't be double-wrapped.
                content = result.output
                tool_obj = self.registry.get(tc.name) if self.registry else None
                if (
                    tool_obj is not None
                    and getattr(tool_obj, "untrusted_source", False)
                    and not _is_envelope_wrapped(content)
                ):
                    content = _wrap_untrusted(tool=tc.name, output=content)
                self.history.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": content,
                    "timestamp": _now_iso(),
                })
                history_written.add(i)
                # P6 Task 5 — escalate-on-failure reflex, signal side.
                # Consecutive-in-order failures of the SAME tool name
                # within this turn: at >=2, surface `(name, count)` via
                # failures_signal so the digest carries an "escalate now"
                # line (rule 11-error-recovery.md reads it). A success
                # clears the streak ONLY when it's the tool actually
                # RECORDED in failures_signal (fix-pass 1, review on
                # commit 99f91c9a) — gating on the local per-turn tracker
                # instead let an unrelated tool's later-turn recovery wipe
                # a still-unresolved streak (the local tracker is reset
                # every turn and gets reassigned to whatever tool runs
                # next, regardless of what's actually recorded).
                #
                # Whole-phase review fix (2026-07-06): scoped to THIS
                # session's `_failures_scope_id` (per-instance, NOT
                # `tool_context.session_id` — see that field's docstring:
                # a forked synthetic session shares its parent's
                # `session_id` but runs concurrently, so scoping on
                # `session_id` would let it clear/collide with the
                # parent's streak) so a concurrent chat's (or fork's)
                # failure/success of the same tool name never leaks into
                # or clears this session's streak.
                scope = self._failures_scope_id
                if result.is_error:
                    if tc.name == self._tool_error_streak_name:
                        self._tool_error_streak_count += 1
                    else:
                        self._tool_error_streak_name = tc.name
                        self._tool_error_streak_count = 1
                    if self._tool_error_streak_count >= 2:
                        from tesseract.brain import failures_signal
                        failures_signal.record_tool_error_streak(
                            tc.name, self._tool_error_streak_count, scope,
                        )
                else:
                    from tesseract.brain import failures_signal
                    recorded = failures_signal.tool_error_streak(scope)
                    if recorded is not None and recorded[0] == tc.name:
                        failures_signal.clear_tool_error_streak(scope)
                    if tc.name == self._tool_error_streak_name:
                        self._tool_error_streak_name = ""
                        self._tool_error_streak_count = 0
        except asyncio.CancelledError as e:
            cancel_error = e
        except Exception as e:
            run_error = e
            logger.exception(
                "tool execution raised non-cancel exception — finally will "
                "append placeholders for unfinished calls",
            )
        finally:
            # Safety net: every pending_call MUST have a matching `role:
            # tool` message in history before this generator returns —
            # otherwise the OpenAI Responses API rejects the next iteration
            # with `400 — No tool output found for function call ...` and
            # the chat is permanently bricked until the orphan is patched.
            # Fires on three paths: cancel, non-cancel exception, AND the
            # success path (no-op there because `history_written` is
            # already complete). Owner caught this 2026-04-29 — a turn
            # with 3 memory_save calls left orphans in the session file
            # that blocked every subsequent chat turn.
            if len(history_written) < len(pending_calls):
                for t in safe_tasks:
                    if not t.done():
                        t.cancel()
                if cancel_error is not None:
                    placeholder = "[cancelled by operator]"
                elif run_error is not None:
                    placeholder = (
                        f"[execution failed: {type(run_error).__name__}: "
                        f"{run_error}]"
                    )
                else:
                    placeholder = (
                        "[interrupted before execution -- placeholder "
                        "injected to satisfy function_call/output pairing "
                        "invariant]"
                    )
                for i, tc in enumerate(pending_calls):
                    if i in history_written:
                        continue
                    if i in results:
                        self.history.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": results[i][1].output,
                            "timestamp": _now_iso(),
                        })
                    else:
                        self.history.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": placeholder,
                            "timestamp": _now_iso(),
                        })
                    history_written.add(i)
        if cancel_error is not None:
            raise cancel_error
        if run_error is not None:
            raise run_error

    def reset(self) -> None:
        self.history.clear()
        self._observer_last_index = 0
        self._pending_suggestions.clear()
        self._pending_conscience.clear()
        # Mark every tracked spawn cancelled BEFORE the queues are cleared and
        # the store discarded, not when the scheduled `cancel_all` below finally
        # reaches it. `cancel_handle` leaves a task that already finished
        # untouched, so without this a spawn completing inside that window sees
        # `cancelled is False` in its done-callback and writes both a note into
        # the just-cleared queue and a durable record into the just-discarded
        # store — resurrecting, at the next restore, work the operator wiped.
        for handle in self.spawns.list_handles():
            handle.cancelled = True
        self._pending_spawn_completions.clear()
        # `/reset` is an explicit wipe, so the durable records go with the
        # queue — left outstanding they would be replayed into the reset chat
        # at the next restore, undoing the operator. An in-flight turn's
        # uncommitted delivery goes too, so its rollback cannot put the wiped
        # notes back.
        self._queued_completion_ids = []
        self._delivering_spawn_blocks = []
        self._delivering_completion_ids = []
        if self.spawns.chat_id:
            from tesseract.brain import completion_store

            completion_store.discard(self.spawns.chat_id)
        self._observed_ids.clear()
        self._turn_injection = ""
        self._consecutive_adapter_errors = 0
        self._tool_error_streak_name = ""
        self._tool_error_streak_count = 0
        self._pending_workspace_comment_ids = []
        self._pending_workspace_event_ids = []
        self.pending_injected_messages = []
        self.tool_context.todos.clear()
        # Phase 4: cancel any background spawns. cancel_all is async
        # so schedule it on the running loop without blocking — `/reset`
        # is a sync command path and the running spawn tasks will
        # honour the cancel and unwind asynchronously. Use
        # get_running_loop (not get_event_loop) so we get the live
        # loop without the 3.10+ deprecation warning, and we naturally
        # raise RuntimeError when called from a synchronous test
        # tear-down path (caught below — nothing to cancel anyway).
        if self.spawns.list_handles():
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.spawns.cancel_all())
            except RuntimeError:
                pass
        if self.interactive_sessions is not None and self.interactive_sessions.list():
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.interactive_sessions.close_all())
            except RuntimeError:
                pass

    def enqueue_user_inject(self, text: str) -> None:
        """Operator typed a follow-up mid-turn. Append for next-tool-boundary
        injection. Caller (Mirror ws.py) holds the asyncio loop alone in
        single-threaded asyncio land, so no lock is needed.
        """
        text = (text or "").strip()
        if not text:
            return
        self.pending_injected_messages.append({
            "text": text,
            "queued_at": _now_iso(),
        })

    def _drain_user_injections(self) -> list[dict[str, Any]]:
        if not self.pending_injected_messages:
            return []
        drained = self.pending_injected_messages
        self.pending_injected_messages = []
        for entry in drained:
            self.history.append({
                "role": "user",
                "content": f"[mid-turn] {entry['text']}",
                "timestamp": _now_iso(),
                "_mid_turn": True,
            })
        return drained

    def token_estimate(self) -> int:
        return self.adapter.count_tokens(self._messages_for_turn())

    def should_compact(self) -> bool:
        """True when history has grown past compact_threshold of context window.

        CR-0: we still gate on a minimum history length so a fresh session
        doesn't compact prematurely. The floor is ``head_anchor + tail + 2``
        where tail is the active window size in messages (legacy fallback)
        or a conservative 4-message minimum when token-budgeted.
        """
        ctx = self.options.context_window or 0
        if ctx <= 0:
            return False
        tail_floor = (
            4 if self.active_window_tokens is not None else self.keep_recent_turns
        )
        if len(self.history) <= self.head_anchor_messages + tail_floor + 2:
            return False
        return self.token_estimate() >= ctx * self.compact_threshold

    async def compact(self) -> tuple[int, int]:
        """Sliding-window compaction with head anchor + append summary.

        Layout after compaction:

            [head_anchor user msgs + interleaving]   # never folded
            [running_summary user msg]               # one msg, grows by append
            [active_tail]                            # token-budgeted

        Returns (tokens_before, tokens_after). No-op (same numbers twice)
        when history is too short or summarization fails.

        CR-0 (2026-05-22) replaced the prior destructive 4-6-sentence
        rewrite.
        """
        before = self.token_estimate()

        # 1. Extract any prior running summary from history. There is at
        #    most one. The remaining list is what we partition into
        #    [anchor, middle, tail] — the summary is re-inserted between
        #    anchor and tail after the new slice is folded in.
        prior_summary_text: str | None = None
        history_clean: list[dict[str, Any]] = []
        for msg in self.history:
            if prior_summary_text is None and _is_running_summary_message(msg):
                prior_summary_text = (msg.get("content") or "")[
                    len(RUNNING_SUMMARY_PREFIX):
                ].strip()
                continue
            history_clean.append(msg)

        head_end = _find_head_anchor_end(history_clean, self.head_anchor_messages)
        head_anchor_msgs = history_clean[:head_end]

        # 2. Compute active tail by token budget (or legacy message-count).
        tail_start = self._compute_tail_start(history_clean, head_end)
        middle = history_clean[head_end:tail_start]
        active_tail = history_clean[tail_start:]

        if not middle:
            # Nothing new to summarize. Common when compaction fires twice
            # in close succession without much new content; just keep the
            # current shape.
            logger.debug("compact: no new middle slice to summarize — no-op")
            return before, before

        new_slice_summary = await compact_history(
            self.adapter,
            self.options,
            middle,
            prior_summary=prior_summary_text,
        )
        if not new_slice_summary:
            logger.warning("compaction returned empty summary — keeping full history")
            return before, before

        # Build the running summary message. Each compaction adds one
        # `# Slice N` H1 block; older blocks survive verbatim.
        slice_n = _next_slice_number(prior_summary_text)
        timestamp = _now_iso()
        new_block = f"# Slice {slice_n} ({timestamp})\n\n{new_slice_summary.strip()}\n"
        if prior_summary_text:
            combined = f"{prior_summary_text.strip()}\n\n{new_block}"
        else:
            combined = new_block
        combined = _trim_summary_to_budget(combined, self.summary_char_budget)
        running_summary_msg = {
            "role": "user",
            "content": f"{RUNNING_SUMMARY_PREFIX}\n\n{combined}",
            "timestamp": timestamp,
        }

        self.history = [
            *head_anchor_msgs,
            running_summary_msg,
            *active_tail,
        ]
        # Pre-compaction turns were already observed (or never will be — the
        # summary is synthetic). Reset the watermark to current end so future
        # _notify_observer_turn_end slices into valid positions.
        self._observer_last_index = len(self.history)
        after = self.token_estimate()
        logger.info(
            "compacted: %d → %d tokens (slice %d, summary %d chars, tail %d msgs)",
            before, after, slice_n, len(combined), len(active_tail),
        )
        return before, after

    # ── compaction helpers ───────────────────────────────────────────

    def _compute_tail_start(
        self, history: list[dict[str, Any]], lower_bound: int
    ) -> int:
        """Return the index from which the verbatim active tail begins.

        When ``active_window_tokens`` is set, walks backwards from the
        end of ``history`` accumulating tokens until the budget is
        reached; otherwise falls back to the legacy ``keep_recent_turns``
        message-count window. ``lower_bound`` is the earliest index the
        tail may start at (i.e. just after the head anchor).
        """
        if self.active_window_tokens is None:
            cutoff = max(lower_bound, len(history) - self.keep_recent_turns)
            return cutoff
        budget = max(0, int(self.active_window_tokens))
        if budget == 0:
            return len(history)
        acc = 0
        tail_start = len(history)
        for i in range(len(history) - 1, lower_bound - 1, -1):
            msg = history[i]
            msg_tokens = self.adapter.count_tokens([msg])
            if acc + msg_tokens > budget and i < len(history) - 1:
                break
            acc += msg_tokens
            tail_start = i
        return max(lower_bound, tail_start)
