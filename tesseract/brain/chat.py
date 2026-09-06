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
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from enum import Enum
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Awaitable, Callable

from tesseract.brain import context_report, context_signal
from tesseract.brain.auto_recall import (
    auto_recall,
    format_recall_block,
    load_auto_recall_config,
    with_connections,
)
from tesseract.brain.compaction import (
    RUNNING_SUMMARY_PREFIX,
    compact_history,
)
from tesseract.brain.completion_store import CompletionRecord, record_from_handle
from tesseract.brain.cost import BudgetExhausted, CostLedger, CostUsage
from tesseract.brain.memory_suggestion import MemorySuggestion
from tesseract.brain.observer_reading import BoundaryNudge, format_for_injection
from tesseract.brain.observation_transcript import ObservationTranscript
from tesseract.brain.spawns import SpawnRegistry
from tesseract.kernel.adapters._estimate import tokens_from_chars
from tesseract.config.loader import (
    DEFAULT_PROMPT_CHAR_BUDGET,
    DEFAULT_COMPACT_RATIO,
    DEFAULT_HEADROOM_MULTIPLIER as _DEFAULT_HEADROOM_MULTIPLIER,
    DEFAULT_KEEP_RECENT_TURNS as _DEFAULT_KEEP_RECENT_TURNS,
)
from tesseract.config.runtime_limits import (
    default_runtime_config_path,
    load_tool_result_window_share,
)
from tesseract.orchestrator.agent_controller.interactive.registry import InteractiveSessionRegistry
from tesseract.orchestrator.outcome import RunOutcome
from tesseract.orchestrator.turns import (
    TurnRecorder,
    enter_turn,
    going_down,
    leave_turn,
    turn_label,
)
from tesseract.brain.tools import AskFn, ToolRegistry, execute_tool
from tesseract.kernel.adapters.base import (
    AdapterOptions,
    CACHE_BOUNDARY,
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

# Imported, not restated. `roles.yaml::compaction.compact_ratio` is the
# setting and boot reads it; this is what a session built without going
# through boot (a sub-agent, a test) gets. The two used to be separate
# numbers, 0.40 here against 0.5 there, and neither was ever reached.
DEFAULT_COMPACT_THRESHOLD = DEFAULT_COMPACT_RATIO
# How far clear of the unfoldable floor the trigger must sit. A fold always
# leaves the head anchor and the verbatim tail behind, so a trigger at or
# below their total cannot get under the line it just crossed and fires again
# every turn. One value for every role: it describes how compaction works,
# not who is using it. `roles.yaml::compaction.headroom_multiplier`.
DEFAULT_HEADROOM_MULTIPLIER = _DEFAULT_HEADROOM_MULTIPLIER
DEFAULT_KEEP_RECENT_TURNS = _DEFAULT_KEEP_RECENT_TURNS
# Sliding window with head anchor + a tail measured in turns.
# `head_anchor_messages` = first N USER messages kept verbatim across every
# compaction (StreamingLLM-style attention sink). `keep_recent_turns` is how
# many recent turns the tail keeps word for word, where a turn is one user
# message and everything the assistant did before the next one — so a turn
# with twenty tool calls is still one turn, and the tail's size is measured
# rather than set. `_tail_ceiling_tokens` is what keeps fewer turns when they
# do not fit.
# `summary_char_budget` caps the running [Context from earlier...] block —
# oldest `# Slice N` block is dropped first when over.
DEFAULT_HEAD_ANCHOR_MESSAGES = 3
DEFAULT_SUMMARY_CHAR_BUDGET = 8_000
PENDING_SUGGESTION_CAP = 8
PENDING_CONSCIENCE_CAP = 4
# Presses inside a card, waiting to ride into the next turn. Bounded where the
# spawn queue is not, and the difference is the source: a spawn result arrives
# once per thing the assistant started, while a person can click a game as fast
# as they like. The card keeps a longer log of its own, so what falls off here
# is still there for `surface_control action:"read"` to fetch.
PENDING_CARD_PRESS_CAP = 8
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

# Hard cap on the assembled prompt sent to ANY adapter in
# the chain. Codex CLI errors at 1_048_576 chars (`input_too_large`);
# leaving ~150 KB headroom for adapter wrapping + output token room.
# `_trim_to_budget` enforces this once at the chokepoint
# (`_messages_for_turn`) so codex, gpt-5.5, gpt-5.4-nano, gemini-2.5-flash
# all receive a payload that fits. See the 2026-05-17 incident on a
# Telegram chat: the prompt grew to 1.88 MB and exhausted every chain
# entry.
#
# This is an EMERGENCY guard, not the thing that bounds a conversation.
# Compaction bounds it, and the char trigger below is what makes that true in
# the char unit as well as the token one. What reaches here is a single turn
# that outgrew the budget between two compactions — one tool loop dumping a
# large file — so it clears re-fetchable tool results first and only drops
# conversation as a last resort.
#
# **The budget is `roles.yaml::compaction`'s** and reaches a session as
# `ChatSession.prompt_char_budget`. It was the one compaction number written in
# source while `compact_ratio`, `headroom_multiplier`, `comfortable_multiplier`,
# `keep_recent_turns`, `head_anchor_messages` and `summary_char_budget` were
# all config, so the one ceiling an operator most needed to move was the one
# they could not.
#
# **One trigger, and it is `compact_ratio`'s.** There used to be two. A char
# trigger sat at `trigger_share` of this budget and `should_compact` folded if
# either arm said yes, so the LOWER of the two decided every fold. On this
# machine that was always the char one: 765,000 chars against a ratio trigger
# of 262,500 tokens, and the last three folds fired at 195,141, 206,165 and
# 233,666 tokens. The operator's dial moved nothing.
#
# The char arm was added for a real reason: a payload used to reach the guard
# before the token trigger fired, and because `token_estimate` then read the
# guard's own trimmed output, the estimate sat at ~30k forever and compaction
# became unreachable (measured 2026-08-23 on a Telegram session: 261,867 input
# tokens, then 30,228 on every call after, for a whole day). Two things answer
# that now without a second trigger. `_assemble_for_turn` is what the estimate
# reads, so it can no longer read the trim. And boot refuses a `compact_ratio`
# whose payload would not fit this budget, naming the highest one that does, so
# a fold above the guard cannot be configured in the first place.

# How many times the guard may trim inside one turn before that is a fault
# rather than a backstop doing its job. Once is the case it was built for: a
# single tool loop dumping one large file. Twice means the conversation is
# being held under the ceiling by the guard rather than by compaction.
_GUARD_LOOP_FAULT_AT = 2

# How many of the most recent tool results the emergency guard leaves alone.
# Clearing a tool result is safe in a way that dropping a turn is not — the
# model can call the tool again, and the conversation that explains WHY is
# what it cannot reconstruct — but the ones it just received are the ones it
# is reasoning about right now.
KEEP_RECENT_TOOL_RESULTS = 6

# How much of a failed tool's output the turn manifest keeps as the step's
# reason. The record is a record of what happened, not a second copy of the
# output — the transcript already holds that, and a manifest that grows with
# tool output is one nobody can read at boot.
_STEP_REASON_CHARS = 400

# Replaces a cleared tool result's content. Says what happened and what to do
# about it, because the model reads this and has to decide whether to re-run
# the tool or carry on.
_CLEARED_TOOL_RESULT = (
    "[tool result cleared to fit the prompt budget. Call the tool again if you "
    "still need its output]"
)

# The per-turn half of the system prompt arrives as a user message, because
# that is the only role every provider lets a caller place after the
# conversation. (Anthropic's newest models accept a trailing `role: "system"`
# message and it would be the non-spoofable channel for this; it is not
# portable, so the sidecar below is what tells the two apart instead.) This
# line says whose words they are, so the assistant does not read its own
# runtime state as something the operator just typed.
_LATE_PROMPT_LEAD = (
    "[runtime_state] The blocks below are this turn's own state, assembled by "
    "the runtime rather than typed by the operator. They were read when this "
    "turn began and replace whatever the previous turn carried.\n\n"
)
# Who wrote a message, on the message. The two synthetic user messages used
# to be recognised by the text they open with, and that text ships in a
# public repo: anyone who can reach `send()` (a channel participant, an
# operator, a tool result read back) could type either banner and have the
# runtime treat their words as its own. `_mid_turn` already showed the
# shape. Stripped before the request goes out, like every other sidecar.
# Only ever compared with itself, inside one process's logs, so a fresh secret
# per boot is exactly right: it makes the conversation tag useless to anyone
# correlating across runs, and costs nothing to whoever is reading one.
_LOG_TAG_KEY = os.urandom(32)

_RUNTIME_KEY = "_runtime"


class Continuation(str, Enum):
    """The two answers a turn may give about the conversation it is in.

    There is one consolidation and it always reflects. What is chosen is only
    what happens afterwards: keep working with the room rebuilt, or leave the
    conversation behind. Whether the boundary was reached because the turn
    judged it or because the window filled changes nothing about the work done
    at it, which is why the trigger is recorded and never branched on.

    Carrying on is the third answer and it is deliberately not here. Declining
    to consolidate is not a mode of consolidating; it is not asking, and a turn
    that says nothing has said it.

    Named here rather than in the tool because the session carries the decision
    and `after_turn` acts on it.
    """

    CONTINUE = "continue"
    RESET = "reset"


_RUNTIME_LATE_PROMPT = "late_prompt"
_RUNTIME_RUNNING_SUMMARY = "running_summary"
# What the last consolidation carried over, written into the conversation it
# cleared. Marked rather than plain for the reason the running summary is: the
# transcript must not draw it wearing the operator's name, and it must not
# open a turn, because nobody said anything.
#
# NOT in `RUNTIME_ORIGINS`. Those name a turn the runtime STARTED; this is
# context placed in front of the next turn, which is the running summary's
# kind and not theirs.
RUNTIME_CONTINUITY = "continuity"
# The origins a caller may claim on `send(runtime_origin=...)`. A turn nobody
# typed carries one, so the transcript can draw it as the runtime speaking and
# a chat's name cannot be taken from it. Anything not in this set is refused
# rather than stored: the value reaches a rendered surface, and a caller that
# invents one there would be inventing a label nothing knows how to draw.
RUNTIME_ORIGINS: frozenset[str] = frozenset({
    "spawn_complete",
    "spawn_stalled",
    "card_press",
    "reflection",
})
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


# ── Compaction helpers ───────────────────────────────────────────────

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
        if not _starts_a_turn(msg):
            continue
        user_count += 1
        if user_count > n_msgs:
            return i
    return len(history)


def _is_late_prompt_message(msg: dict[str, Any]) -> bool:
    """True for the transient message carrying the prompt's late half.

    Assembled fresh every turn and never stored, so no fold can remove it,
    and so it has no legacy form: the only one that has ever existed is the
    one `_build_messages` just made. Nothing here reads content, which is
    what stops a stored message that opens with the same banner from being
    dropped out of the fold trigger's accounting.
    """
    return msg.get(_RUNTIME_KEY) == _RUNTIME_LATE_PROMPT


def _starts_a_turn(msg: dict[str, Any]) -> bool:
    """True when ``msg`` opens a turn.

    A turn is one thing the operator said and everything that followed before
    they said the next thing: the assistant's tool calls, their results, the
    reply. So a turn with twenty tool calls is one turn, and the boundary is
    the operator's message. The running summary is synthetic and a mid-turn
    injection lands inside a turn already open, so neither opens one.
    """
    return (
        msg.get("role") == "user"
        and not _is_running_summary_message(msg)
        and msg.get(_RUNTIME_KEY) != RUNTIME_CONTINUITY
        and not msg.get("_mid_turn")
    )


def _turn_starts(history: list[dict[str, Any]], lower_bound: int = 0) -> list[int]:
    """Indices in ``history`` at or after ``lower_bound`` that open a turn."""
    return [
        i for i in range(lower_bound, len(history)) if _starts_a_turn(history[i])
    ]


def _is_running_summary_message(msg: dict[str, Any]) -> bool:
    """True only for a summary this runtime wrote.

    The marker and nothing else. Accepting the tag as well looked harmless,
    because a summary is a summary whoever wrote it, but the tag is a fixed
    string in a public repo and this answer decides four things: whether the
    message opens a turn, whether it counts toward the head anchor, whether a
    channel forwards it into the rolling summary, and whether the transcript
    draws it as a divider instead of as what somebody said. A participant who
    types the tag got all four, which means they could make their own message
    disappear from a reloaded conversation.

    The cost is one fold: a conversation compacted before the marker existed
    has a summary with the tag and no marker, so its next fold treats it as
    ordinary content, folds it back in, and starts slice numbering again.
    Nothing is lost, the text is summarised like any other, and after that
    fold the conversation carries a marked summary like every other.
    """
    return msg.get(_RUNTIME_KEY) == _RUNTIME_RUNNING_SUMMARY


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
# a small-window entry) is the primary defense; this audit is a backstop.
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


# The estimator every adapter uses counts four characters to a token, so a
# window in tokens becomes a budget in characters the same way.
_CHARS_PER_TOKEN = 4


# Resolved on first use, not per tool call. `None` means not yet read.
_RESULT_WINDOW_SHARE: float | None = None


def _result_window_share() -> float:
    """`tool_result_window_share`, read once per process and cached.

    Two things this must not do, both learned by doing them. It must not read
    the file on every tool call, which is a YAML parse per call for a number
    that changes at the speed an operator edits a file. And it must not let a
    missing config end the turn: the ceiling is a backstop, so a runtime that
    cannot read it should lose the backstop and say so, not lose the ability
    to call tools at all. Eight tests died mid-turn on a
    `FileNotFoundError` from an isolated `TESSERACT_HOME`, which is exactly
    what an installed app looks like before first-run setup writes its config.
    """
    global _RESULT_WINDOW_SHARE
    if _RESULT_WINDOW_SHARE is None:
        try:
            _RESULT_WINDOW_SHARE = load_tool_result_window_share(
                default_runtime_config_path()
            )
        except (OSError, ValueError) as exc:
            logger.warning(
                "tool result ceiling is off: %s. A single oversized tool "
                "result can now make the next request too large for every "
                "model in the chain.", exc,
            )
            _RESULT_WINDOW_SHARE = 1.0
    return _RESULT_WINDOW_SHARE


def bound_tool_result(
    result: ToolResult,
    tool_name: str,
    context_window: int,
    share: float,
) -> ToolResult:
    """Cut a tool result down to `share` of the model's context window.

    Each tool bounds what it returns; this bounds what ANY tool can put in
    front of the model, which is a different job. A single grep once came
    back 44 MB, which made the next request larger than every window in the
    chain, so the session could not reach a model at all. Nothing downstream
    could recover it either: `_trim_history` clears OLDER tool results and
    must leave the newest one alone, because a tool call with no result
    breaks the pairing every provider requires. So the cut happens here, on
    the way in, or it does not happen.

    The notice carries the real numbers and what to do about them, because
    the model reading it is the one that can act: run the call again over
    something smaller.
    """
    if share >= 1 or context_window <= 0:
        return result
    limit = int(context_window * _CHARS_PER_TOKEN * share)
    if limit <= 0 or len(result.output) <= limit:
        return result
    notice = (
        f"\n\n[Cut here. {tool_name} returned {len(result.output):,} characters "
        f"and the first {limit:,} are above. One tool result may not take more "
        f"than {share:.0%} of this model's context window, or the next request "
        "cannot be sent at all. Run the call again over something smaller, or "
        "ask the tool for fewer characters, and it will come back whole.]"
    )
    logger.warning(
        "tool result from %s cut: %d chars over the %d-char ceiling "
        "(%.0f%% of a %d-token window)",
        tool_name, len(result.output), limit, share * 100, context_window,
    )
    return dataclasses.replace(result, output=result.output[:limit] + notice)


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


def _clear_old_tool_results(
    messages: list[dict[str, Any]],
    *,
    char_limit: int,
    protected: frozenset[int],
) -> tuple[list[dict[str, Any]], int]:
    """Blank the content of the oldest tool results until the payload fits.

    The message STAYS in place. A ``tool`` message answers an assistant
    ``tool_calls`` message by id, and removing one leaves a call with no
    result, which every provider in the chain rejects. Content is what costs
    and content is what goes, so the pairing survives.

    Why this runs before anything else is dropped: a tool result is
    re-fetchable — the model can call the tool again — and the conversation
    explaining why it called it is not. The old order had that backwards.

    Returns the list (unchanged object when nothing was cleared) and how many
    results it cleared.
    """
    tool_idx = [
        i for i, m in enumerate(messages)
        if m.get("role") == "tool" and i not in protected
    ]
    if not tool_idx:
        return messages, 0
    # Two passes, and the second one is why a reserve cannot be an absolute
    # count. Six untouched results at 100k each are 600k on their own, so
    # holding them back means the budget is met by dropping CONVERSATION
    # instead — the exact trade this function exists to refuse. So the
    # reserve is a preference, not a floor: everything outside it goes first,
    # and if that is still not enough the reserve goes too, oldest first.
    # The single newest result is never cleared. That one is what the model
    # is reasoning about right now, and a turn that cannot see the result it
    # just asked for has nothing to say.
    preferred = tool_idx[:-KEEP_RECENT_TOOL_RESULTS]
    reserve = tool_idx[max(0, len(tool_idx) - KEEP_RECENT_TOOL_RESULTS):-1]
    total = sum(_content_chars(m) for m in messages)
    out = list(messages)
    cleared = 0
    for i in [*preferred, *reserve]:
        if total <= char_limit:
            break
        was = _content_chars(out[i])
        if was <= len(_CLEARED_TOOL_RESULT):
            continue
        out[i] = {**out[i], "content": _CLEARED_TOOL_RESULT}
        total -= was - len(_CLEARED_TOOL_RESULT)
        cleared += 1
    if not cleared:
        return messages, 0
    return out, cleared


def _say_it_was_trimmed(out: list[dict[str, Any]]) -> None:
    """Put the "history was trimmed" note at the front of what is left.

    One function because there were two copies, five lines apart in the same
    routine, and a note that told the model its history had been cut is the
    kind of thing that goes silently missing from one of two copies.

    `role: assistant` and not `system`: Gemini rejects multiple system
    messages in one call and is in our fallback chain, and an assistant-role
    note reads naturally across every provider.
    """
    insert_at = 1 if (out and out[0].get("role") == "system") else 0
    out.insert(
        insert_at,
        {"role": "assistant", "content": _TRIM_HISTORY_MARKER.strip()},
    )


def _trim_to_budget(
    messages: list[dict[str, Any]],
    *,
    char_limit: int,
    protected: frozenset[int] = frozenset(),
) -> list[dict[str, Any]]:
    """Enforce a hard char budget on the assembled message list.

    ``protected`` names indices this must never drop: the per-turn half of
    the prompt and the one-shot injection, which are runtime state rather
    than conversation. They used to be inside the system message, which was
    exempt by being index 0; riding as their own messages made them droppable,
    and a trim mid-tool-loop dropped exactly them.

    This is the emergency guard, and reaching it means compaction did not get
    there first — see ``compact_char_trigger``. Every step below is therefore
    logged at WARNING, including the ones that used to return silently: a
    payload that quietly stopped carrying the conversation is the failure mode
    this guard existed to prevent, and it caused it once already.

    Drop order, most aggressive last:
      1. Clear tool results, oldest first, preferring the ones outside
         the newest KEEP_RECENT_TOOL_RESULTS and escalating into them if
         that is not enough. Re-fetchable, so it goes first, and it goes
         as far as it has to before any conversation is touched.
      2. Oldest history entries (preserve system, protected, last
         KEEP_LAST_TURNS real turns).
      3. Shrink the ``<recall_context>`` block, in whichever message carries
         it — a channel prepends it to the operator's own text, which is not
         the last message in the list.
      4. (Last resort) emit a warning — adapter will reject. We never drop
         the newest conversation message; that's the operator's actual ask.
         Newest CONVERSATION, not last slot: the per-turn runtime blocks ride
         after it.

    Idempotent on already-small payloads.
    """
    before_chars = sum(_content_chars(m) for m in messages)
    if before_chars <= char_limit:
        return messages

    messages, cleared = _clear_old_tool_results(
        messages, char_limit=char_limit, protected=protected,
    )
    total = sum(_content_chars(m) for m in messages)
    if total <= char_limit:
        logger.warning(
            "prompt guard: cleared %d old tool result(s), %d->%d chars "
            "(limit %d) — conversation intact",
            cleared, before_chars, total, char_limit,
        )
        return messages

    keep_indices: set[int] = set(protected)
    if messages and messages[0].get("role") == "system":
        keep_indices.add(0)
    # Count back real turns, skipping what is already kept. Taking the last
    # KEEP_LAST_TURNS *slots* was right while every slot was a turn; the
    # runtime blocks now occupy slots too, and counting them cost a real turn
    # early in a tool loop and dropped the blocks themselves later in one.
    kept = 0
    for i in range(len(messages) - 1, -1, -1):
        if kept >= KEEP_LAST_TURNS:
            break
        if i in keep_indices:
            continue
        keep_indices.add(i)
        kept += 1

    # A `tool` message answers an assistant `tool_calls` message by id, and a
    # provider rejects both halves of a broken pairing: a result whose call is
    # gone, and a call whose result is gone. The countback above counts message
    # SLOTS, so it can end anywhere in a tool loop and break either half.
    # Pre-existing — the old fixed-range loop had the same blind spot and no
    # test carried a tool message — and closed here by repairing to a fixpoint.
    #
    # A fixpoint rather than one pass because the two repairs feed each other:
    # keeping a result pulls in its assistant message, and that message may
    # carry three OTHER calls whose results are outside the window, each of
    # which has to come back too. One forward pass pulled in the message and
    # left those three calls unanswered, which is the shape that fails.
    countback_kept = frozenset(keep_indices)
    while True:
        kept_call_ids: set[str] = set()
        for i in keep_indices:
            for call in messages[i].get("tool_calls") or []:
                cid = (call or {}).get("id")
                if cid:
                    kept_call_ids.add(cid)
            if messages[i].get("role") == "tool":
                cid = messages[i].get("tool_call_id")
                if cid:
                    kept_call_ids.add(cid)
        grew = False
        for i, m in enumerate(messages):
            if i in keep_indices:
                continue
            if m.get("role") == "tool":
                match = m.get("tool_call_id") in kept_call_ids
            else:
                match = any(
                    (c or {}).get("id") in kept_call_ids
                    for c in m.get("tool_calls") or []
                )
            if match:
                keep_indices.add(i)
                grew = True
        if not grew:
            break

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
        _say_it_was_trimmed(out)

    total = sum(_content_chars(m) for m in out)
    repair_added = keep_indices - countback_kept
    if total > char_limit and repair_added:
        # The pairing repair adds messages back AFTER the countback decided the
        # budget, so the payload can come back over it. Drop whole tool loops
        # from the oldest end — a loop, not a message, or the repair simply
        # pulls the message back in on the next call.
        #
        # Only what the REPAIR added is eligible. The countback's own last
        # KEEP_LAST_TURNS are the contract this function has always kept, and
        # an earlier cut of this claw-back was written as "over budget" rather
        # than "over budget BECAUSE of the repair" — which dropped a real turn
        # on a payload that had no tool traffic in it at all. When the last
        # turns alone exceed the budget the answer is still the warning below,
        # not a shorter conversation.
        essential = set(protected)
        if messages and messages[0].get("role") == "system":
            essential.add(0)
        # The newest CONVERSATION message, which is not the last slot any
        # more: the per-turn runtime blocks ride after it, and they are in
        # `protected` already, so adding `len(messages) - 1` re-added one of
        # them and left the operator's own ask covered by nothing but the
        # countback happening to reach it.
        newest = next(
            (i for i in range(len(messages) - 1, -1, -1) if i not in essential),
            None,
        )
        if newest is not None:
            essential.add(newest)
        for i in sorted(repair_added):
            if total <= char_limit:
                break
            if i not in keep_indices or i in essential:
                continue
            loop = {i}
            for call in messages[i].get("tool_calls") or []:
                cid = (call or {}).get("id")
                loop |= {
                    j for j, m in enumerate(messages)
                    if m.get("tool_call_id") == cid and cid
                }
            cid = messages[i].get("tool_call_id")
            if cid:
                loop |= {
                    j for j, m in enumerate(messages)
                    if any((c or {}).get("id") == cid
                           for c in m.get("tool_calls") or [])
                }
            loop -= essential
            if not loop:
                continue
            keep_indices -= loop
            total -= sum(_content_chars(messages[j]) for j in loop)
            dropped += len(loop)
        out = [m for j, m in enumerate(messages) if j in keep_indices]
        _say_it_was_trimmed(out)
        total = sum(_content_chars(m) for m in out)

    if total <= char_limit:
        # Was a bare `return out`. Dropping conversation is the most
        # destructive thing this function does and it was the ONE exit that
        # said nothing, so the day it fired there was no line anywhere saying
        # the assistant had stopped carrying its own conversation.
        logger.warning(
            "prompt guard: dropped %d conversation message(s) after clearing "
            "tool results, %d->%d chars (limit %d) — continuity is degraded, "
            "recover with recall_history",
            dropped, before_chars, total, char_limit,
        )
        return out

    # Step 3: shrink the recall_context block, wherever it is.
    #
    # Found by position rather than assumed to be last. A channel prepends the
    # block to the operator's own message, and that message stopped being the
    # end of the list when the per-turn state moved below it — so reading
    # `out[-1]` shrank nothing at all and the guard skipped a whole step in
    # silence. Searched backwards because the newest one is the turn's own.
    at = next(
        (
            i
            for i in range(len(out) - 1, -1, -1)
            if isinstance(out[i].get("content"), str)
            and _RECALL_OPEN in out[i]["content"]
        ),
        None,
    )
    if at is not None:
        content = out[at]["content"]
        budget_for_it = max(
            RECALL_CONTEXT_MIN_KEEP,
            char_limit - sum(_content_chars(m) for i, m in enumerate(out) if i != at),
        )
        out[at] = {**out[at], "content": _shrink_recall_block(content, target_len=budget_for_it)}

    total = sum(_content_chars(m) for m in out)
    if total > char_limit:
        logger.warning(
            "prompt budget %d exceeded after trim: %d chars remaining",
            char_limit, total,
        )
    else:
        logger.warning(
            "prompt guard: dropped %d conversation message(s) and shortened "
            "recall, %d->%d chars (limit %d) — continuity is degraded",
            dropped, before_chars, total, char_limit,
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

    This does NOT mark the comments delivered. The caller calls
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


# Ambient observer: defence-in-depth secret redaction. The Mirror
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


def _format_completion_record(
    record: CompletionRecord, *, replayed: bool = False
) -> str:
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
    after a restart — when the handle is long gone — renders the same way as
    one delivered live. Queued by ``ChatSession.ingest_spawn_completion`` or
    ``replay_undelivered_completions`` and surfaced on the next turn's
    iteration 0 (same one-shot semantics as conscience notes).

    **Except for its age, which it must not hide.** The block used to carry
    handle, kind and status and nothing else, so a result recorded three days
    ago read exactly like one from three seconds ago. On 2026-08-24 that is
    precisely what happened: an audit from 2026-08-21 was replayed into a
    reopened chat and acted on as current, and the assistant went looking for
    a file the revert had deleted two days earlier. So the header states when
    it finished, and ``replayed`` says outright that this came off disk rather
    than from a handle this session started, because that is the case where
    the tree has had time to move underneath it.
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
    from tesseract.brain.prompt_time import age_from_iso

    finished = age_from_iso(record.finished_at or "")
    header = (
        f"[spawn_completed] handle={handle_id} kind={kind} status={status} "
        f"finished={finished}"
    )
    if replayed:
        header += (
            "\nThis was recorded before now and never read. Check the current "
            "state before acting on it: the working tree may have moved since."
        )
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
    headroom_multiplier: float = DEFAULT_HEADROOM_MULTIPLIER
    keep_recent_turns: int = DEFAULT_KEEP_RECENT_TURNS
    # Sliding-window knobs. See the module docstring.
    head_anchor_messages: int = DEFAULT_HEAD_ANCHOR_MESSAGES
    summary_char_budget: int = DEFAULT_SUMMARY_CHAR_BUDGET
    ask_fn: AskFn | None = None  # operator approval callback for ASK-permission tools
    policy: PermissionPolicy | None = None  # config-driven per-tool posture
    # When set, called per turn to re-assemble the system prompt — lets
    # SOUL.md edits land inside the active session instead of
    # waiting for the next one. If unset, the frozen `system_prompt` is used.
    # Channel sessions plumb a channel-aware builder here from
    # ``_build_chat_session``; the channel overlay is inlined inside the
    # assembled manifest (cacheable prefix) rather than appended at runtime.
    prompt_builder: Callable[[], str] | None = None
    # Set to ``"channel"`` for sessions built by a channel adapter,
    # otherwise ``"cockpit"``. Not load-bearing for the prompt (the
    # builder already encodes the overlay); the gate reads it to choose
    # between operator-attended ASK and the workspace-nudge path.
    # ``channel_display_name`` mirrors the value handed to the
    # channel-aware ``prompt_builder`` for the same reason.
    session_kind: str = "cockpit"
    channel_display_name: str | None = None
    # The funnel door this session's turns come through — a node name from
    # `orchestrator/funnel.py::NODES`, so a turn record joins to the map the
    # floor plan already draws rather than inventing a second vocabulary for
    # the same four doors. Set only by `session_factory._build_chat_session`,
    # which is the one seam the cockpit and every channel are built through.
    # Empty means this session's turns are not recorded: sub-agent sessions,
    # the REPL and tests all build a ChatSession without going near that seam,
    # and a manifest per synthetic turn would be noise rather than a record.
    turn_entry: str = ""
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
    # One slot, not a queue. A nudge says how this conversation stands right
    # now, so an undelivered one is worthless the moment a newer reading
    # replaces it — a backlog of them would report on a turn already left
    # behind.
    # Bumped every time this object starts holding a different conversation.
    # Work that was launched against the old one and lands after the wipe can
    # compare what it captured against what is current and stand down. The
    # observer runs as a detached task, so this is not a theoretical race: its
    # model call outlives the turn that started it by design.
    _conversation_generation: int = field(default=0, repr=False)
    _tool_failure_limit_cache: int | None = field(default=None, repr=False)
    _pending_nudge: BoundaryNudge | None = field(default=None, repr=False)
    _last_nudge_observation_id: str | None = field(default=None, repr=False)
    # What was actually shown, held until the end of that turn so the boundary
    # can record what came of it. A nudge nobody can score is a nudge nobody
    # can tell is being ignored.
    _delivered_nudge: BoundaryNudge | None = field(default=None, repr=False)
    _pending_conscience: deque[str] = field(
        default_factory=lambda: deque(maxlen=PENDING_CONSCIENCE_CAP),
        repr=False,
    )
    _pending_spawn_completions: deque[str] = field(
        default_factory=deque,
        repr=False,
    )
    #: Controls the operator used inside a card this chat drew. Same one-shot
    #: shape as the conscience queue, and capped for the same reason: someone
    #: clicking through a game while the assistant is mid-turn must not build a
    #: block longer than the turn it lands in. The card keeps its own longer
    #: log, which `surface_control action:"read"` returns, so the overflow is
    #: still there to be asked for.
    _pending_card_presses: deque[str] = field(
        default_factory=lambda: deque(maxlen=PENDING_CARD_PRESS_CAP),
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
    # Whether a turn is running. It was `_turn_anchor_idx`, the index of this
    # turn's own user message, back when the per-turn blocks were inserted in
    # front of it; those go after the whole list now, so nothing read the
    # number as an index any more and every remaining use was `>= 0`. A bool
    # named for the question it answers, because an int called an index is a
    # standing invitation to use it as one.
    #
    # What it decides: whether this turn's reading of the prompt is worth
    # holding (`_turn_prompt` below).
    _turn_active: bool = field(default=False, repr=False)
    # This turn's one reading of the prompt, head and volatile half together.
    # Held so every tool-loop iteration sends the same bytes; `None` between
    # turns, so a `token_estimate()` taken then builds fresh and caches
    # nothing into the next turn. A `PromptParts` survives the round trip
    # because it IS a `str` — the annotation is honest, not narrowing.
    # What is cached is whatever the builder returned, INCLUDING the frozen
    # fallback it returns when assembly fails, so one failed read pins that
    # fallback for the turn. Deliberate: rebuilding mid-loop would change
    # bytes the previous iteration paid to cache, and would flap the model's
    # instructions between two prompts inside one answer.
    _turn_prompt: str | None = field(default=None, repr=False)
    # The head as of the last turn, kept only to notice when it moves. The
    # head is `instructions` on the wire, which sits ahead of the tools and
    # the whole conversation and cannot carry a cache breakpoint, so a single
    # byte moving in it re-reads everything at full price.
    _last_head: str | None = field(default=None, repr=False)
    # How many turns rebuilt a head that differs from the turn before's.
    # The cost of what is NOT held, counted rather than assumed.
    _head_drift: int = field(default=0, repr=False)
    # The bytes of the held sections, as this conversation first read them.
    # Which sections those are is `prompt.HELD_SECTION_NAMES`, declared beside
    # each builder and derived from the roster, never listed again here.
    #
    # A name absent from this dict was absent from the prompt on the turn the
    # snapshot was taken, and stays absent: a capsule that shows up on turn 6
    # because a job finally wrote one would move the head exactly as a changed
    # capsule does, and the hold has to cover both or it covers neither.
    #
    # `None` means the next turn takes the snapshot, which is what a new
    # conversation and a fold both leave behind.
    _held_sections: dict[str, str] | None = field(default=None, repr=False)
    # Which conversation the snapshot above was taken in, per
    # `_conversation_id`. A `ChatSession` can be handed another conversation's
    # history while it goes on living — `commands.py`'s batch compaction does
    # exactly that — so a hold keyed to the object would outlive the
    # conversation it was taken for.
    _held_for: str | None = field(default=None, repr=False)
    # The head revision the snapshot was taken at, per
    # `prompt.head_revision()`. The hold covers what moves with nobody asking;
    # this is what lets an operator ACT move it anyway, on the turn after the
    # act, without the head being rebuilt every turn on the chance that one
    # happened.
    _held_revision: int | None = field(default=None, repr=False)
    # The shape of the request this iteration sent, stashed where it was
    # built. `_log_cache_shape` reads it; nothing else does.
    _last_request_shape: tuple[str, int, int, int, int] | None = field(
        default=None, repr=False
    )
    # The system message as the last fold decision measured it. Stashed
    # rather than recounted, because everything that publishes it was
    # counting `system_prompt` and that is the string frozen when the
    # session was built, not the one a `prompt_builder` rebuilds each turn.
    _system_tokens: int = field(default=0, repr=False)
    # Why the last fold came back with the tokens unchanged. `compact` returns
    # `(before, before)` for two unrelated reasons and the caller could not
    # tell them apart, so `/compact` told the operator their chat still fits
    # when what actually happened was the summarizer failing on a chat that
    # does not. One of "folded", "nothing_to_fold", "summarizer_failed".
    _last_fold_outcome: str = field(default="folded", repr=False)
    # Turns the last fold kept word for word, so whoever reports the fold can
    # say where its boundary was. Internal state like the two below it, not a
    # setting: it belongs beside them rather than among the operator's knobs.
    _last_fold_tail_turns: int = field(default=0, repr=False)
    _observer_subscriber: Any | None = field(default=None, repr=False)
    _observer_last_index: int = field(default=0, repr=False)
    # This conversation's own rolling window into the observer. It lives here
    # rather than on the observer so every entry point that builds a session
    # gets one by construction, and two conversations observed at once cannot
    # interleave. Dies with the session — nothing evicts it.
    _observer_transcript: ObservationTranscript = field(
        default_factory=ObservationTranscript, repr=False
    )
    # The cockpit's suggestion chip. A channel has no counterpart and leaves
    # this `None`; the injection into the next turn is surface-independent and
    # happens either way.
    _observer_emit: Any | None = field(default=None, repr=False)
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
    # Operator-typed messages that arrive while a turn
    # is mid-flight. The WS appends here. The tool loop drains this list
    # between iterations (inside `send`, after
    # `_run_pending_calls` yields the last result chunk) and folds each
    # entry into history as a `role: user` message with `[mid-turn]`
    # framing so the model can pivot. A USER_INJECT StreamChunk is
    # yielded so the WS can fire `stream_user_inject` and clear the
    # frontend "queued" badge.
    pending_injected_messages: list[dict[str, Any]] = field(default_factory=list, repr=False)
    # Background-spawn registry. Tools that take
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
    # The manifest of the turn currently running, or None between turns.
    # Opened at the top of `send()` and closed in its `finally`, so every exit
    # path — a clean return, an operator cancel, a shutdown that kills the
    # process — leaves the record in the state that exit means.
    _turn_recorder: TurnRecorder | None = field(default=None, repr=False)
    #: How the last turn on this session ended, and why. Set on every exit
    #: path, whether or not the session records turns, because the answer is
    #: what a surface needs in order to say something better than "no reply".
    #: One field pair, not one per turn: turns on a session are serial.
    last_turn_outcome: RunOutcome | None = field(default=None, repr=False)
    last_turn_reason: str = field(default="", repr=False)
    #: Which turn that was. Beside the pair above and set the same way, for a
    #: surface that has already said something to the person and needs to note
    #: it on the record so nobody says it again. `_turn_recorder` is cleared on
    #: close and a shutdown leaves the record open, so the id has to outlive
    #: the recorder. Empty on a session that records no turns.
    last_turn_id: str = field(default="", repr=False)
    #: The two char ceilings, from `roles.yaml::compaction`. Defaulted because
    #: a session built outside the funnel (a sub-agent, the REPL, a test) still
    #: needs a guard, and `config/loader.py` is where the one shipped answer
    #: lives.
    prompt_char_budget: int = DEFAULT_PROMPT_CHAR_BUDGET
    #: How many times the emergency prompt guard has trimmed inside the turn
    #: currently running. Reset at the top of `send`. Twice is a fault, and
    #: `_messages_for_turn` says so once.
    _guard_firings_this_turn: int = field(default=0, repr=False)
    #: Every time the guard has trimmed in this session, never reset. The
    #: per-turn count above drives the fault log and is gone the next turn, so
    #: nothing could answer "has the guard been holding this conversation
    #: down?" from any surface. `context_report` reads this one.
    _guard_firings_this_session: int = field(default=0, repr=False)
    #: What this turn asked to happen to itself once it is over: "continue",
    #: "reset", or "" for carrying on, which is the ordinary case. Written by
    #: `session_continue` through `ToolContext.request_continuation` and read
    #: at the turn boundary by `after_turn`, because `compact()` and `reset()`
    #: rewrite `history` in place: acting on it inside the turn folds away the
    #: assistant message carrying the pending `tool_use` block before its
    #: `tool_result` is appended, and the next request is malformed.
    _continuation: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        # Cross-link the spawn registry into ToolContext so tools can
        # reach it without importing brain-layer modules. Runs once at
        # ChatSession construction; `tool_context` is the same instance
        # for the session's lifetime, so this assignment doesn't get
        # invalidated by `rebuild_adapters` etc.
        self.tool_context.spawns = self.spawns
        self.tool_context.interactive_sessions = self.interactive_sessions
        self.tool_context.enabled_extended_tools = self._enabled_extended_tools
        # How full this conversation is, reachable by a tool. Everything it
        # reports was already measured for the cockpit's HUD and could be read
        # nowhere else, so the same question failed on a channel and in the
        # cockpit's own chat.
        self.tool_context.context_report = lambda: context_report.gather(self)
        # And what it may ask to happen once the turn is over. A callable for
        # the same reason `context_report` is one: a tool deciding this
        # conversation is finished has no business reaching its history.
        self.tool_context.request_continuation = self.request_continuation
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

    def _record_step(
        self,
        *,
        kind: str,
        name: str,
        outcome: RunOutcome,
        reason: str = "",
        started_at: datetime | None = None,
    ) -> None:
        """One step of the running turn, committed to its manifest.

        A no-op when the session records no turns, so every call site reads the
        same whether or not this session came through the funnel's seam."""
        if self._turn_recorder is None:
            return
        self._turn_recorder.step(
            kind=kind, name=name, outcome=outcome, reason=reason, started_at=started_at,
        )

    @property
    def _turn_label(self) -> str:
        """What to call this turn on a surface, in the door's own words.

        The words are `turns.manifest.turn_label`'s, read off the record's own
        entry, so the liveness row and the atlas node name one turn one way.
        Per AR-8's rule every string a pane renders about the machine comes
        from the backend, and never the operator's own text."""
        return turn_label(self._turn_record_entry)

    @property
    def _turn_record_entry(self) -> str:
        """The door, and which one of it, as the turn's record names it. The
        funnel declares `channel` as a single node because every channel is the
        same door, and a record still has to say which one a person was waiting
        on. The bridge stamps `tool_context.channel` right after building the
        session."""
        entry = self.turn_entry or ""
        if entry and self.tool_context.channel:
            entry = f"{entry}:{self.tool_context.channel}"
        return entry

    def _beat(self, waiting_on: str) -> None:
        """Say the turn is alive and what it is now waiting on.

        Called where a step BEGINS, not where one is recorded. `_record_step`
        above commits what a step did; this says what the turn is doing, which
        is the only one of the two a person waiting can use. The registry's
        `register` is an upsert that stamps `updated_at`, so the last call here
        is the last beat, and a turn wedged inside one slow call stops beating
        exactly while it looks busiest.

        A no-op when the session records no turns, like `_record_step`."""
        if self._turn_recorder is None:
            return
        from tesseract.orchestrator.activity.hooks import register_turn

        register_turn(
            self._turn_recorder.turn_id,
            label=self._turn_label,
            waiting_on=waiting_on,
        )

    def _close_turn_record(self, outcome: RunOutcome, reason: str = "") -> None:
        if self._turn_recorder is None:
            return
        from tesseract.orchestrator.activity.hooks import remove_turn

        recorder, self._turn_recorder = self._turn_recorder, None
        leave_turn()
        remove_turn(recorder.turn_id)
        recorder.close(outcome, reason)
        # A turn the process is stopping under stays open, and the next boot
        # writes its end onto the task as `truncated`; writing it here too
        # would give one turn two attempts that disagree.
        if recorder.manifest.task_id and not going_down():
            from tesseract.orchestrator.turns.tasks import note_turn_ended

            note_turn_ended(recorder.manifest, outcome, reason)

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

        A synthetic workspace turn runs concurrently with the chat turn.
        It must NOT mutate the canonical history (the workspace reply is
        delivered via the `workspace_reply` tool, not via the chat
        conversation), and it must NOT share mutable state with the chat
        session.

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
            shared-mutable state-holders on the tool instance).
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
            headroom_multiplier=self.headroom_multiplier,
            keep_recent_turns=self.keep_recent_turns,
            head_anchor_messages=self.head_anchor_messages,
            summary_char_budget=self.summary_char_budget,
            # The fork reuses this session's adapter, so it talks to the same
            # model and needs the same ceiling. Left off, it took the dataclass
            # default and a synthetic turn on a tight model was guarded by a
            # number belonging to nothing it was about to send to.
            prompt_char_budget=self.prompt_char_budget,
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

    def set_observer_emit(self, emit_fn: Any | None) -> None:
        self._observer_emit = emit_fn

    @property
    def observer_transcript(self) -> ObservationTranscript:
        return self._observer_transcript

    @property
    def conversation_generation(self) -> int:
        """Which conversation this object is holding right now.

        Read it before starting slow work against a session and again before
        acting on the result. A different number means the conversation the
        work was about has been cleared and the result describes nothing.
        """
        return self._conversation_generation

    @property
    def observer_emit(self) -> Any | None:
        return self._observer_emit

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

    def _tool_failure_limit(self) -> int:
        """How many consecutive failures of one tool is a boundary.

        Cached for the life of the turn. The config watcher rebuilds adapters
        on an edit, so a change lands on the next turn rather than mid-way
        through this one, which is the right granularity for a rule about a
        whole turn.

        Falls back to refusing to promote, never to a guessed number: an
        unreadable bound is not evidence that a tool is failing, and stopping
        a conversation on a parse error would be the worst of both.
        """
        if self._tool_failure_limit_cache is None:
            from tesseract.brain.continuity import load_boundary_bounds

            try:
                self._tool_failure_limit_cache = load_boundary_bounds().tool_failure_limit
            except Exception:
                logger.exception("could not read the tool failure limit; not promoting")
                self._tool_failure_limit_cache = 0
        return self._tool_failure_limit_cache or (1 << 30)

    def ingest_boundary_nudge(self, nudge: BoundaryNudge) -> bool:
        """Hold the observer's boundary recommendation for the next turn.

        Returns `False` for a repeat of the observation already held, which is
        what stops one re-fired observation being announced twice. Deduped on
        its own id rather than `_observed_ids`, because the suggestion from the
        same call carries the same `observation_id` and would otherwise swallow
        the nudge whenever both halves arrived together.
        """
        if nudge.observation_id == self._last_nudge_observation_id:
            return False
        self._last_nudge_observation_id = nudge.observation_id
        self._pending_nudge = nudge
        return True

    def take_delivered_nudge(self) -> BoundaryNudge | None:
        """The nudge shown this turn, cleared as it is read.

        One reader, at the end of the turn, which is the only moment both the
        recommendation and the answer to it are known.
        """
        nudge = self._delivered_nudge
        self._delivered_nudge = None
        return nudge

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

    def ingest_card_press(self, surface_id: str, target: str, value: Any = None) -> None:
        """Queue a control the operator used inside a card this chat drew.

        The other half of the canvas bridge. A card the assistant authored
        reports its presses, and until this they were only readable: the
        assistant had to think to look. Now the fact rides into the next turn
        the way a finished spawn does, and `mirror/server/card_wake.py` decides
        separately whether to start that turn rather than wait for one.

        One-shot and never persisted to history, like every sibling queue here.
        """
        said = f" = {value}" if value not in (None, "") else ""
        self._pending_card_presses.append(
            f"[card_pressed] surface={surface_id} control={target}{said}"
        )

    def has_pending_card_presses(self) -> bool:
        return bool(self._pending_card_presses)

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
            self._pending_spawn_completions.append(
                _format_completion_record(record, replayed=True)
            )
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
        # `_drain_user_injections`, and providers reject
        # unknown message fields.
        if (
            "timestamp" not in message
            and "_meta" not in message
            and "_mid_turn" not in message
            and _RUNTIME_KEY not in message
        ):
            return message
        clean = dict(message)
        clean.pop("timestamp", None)
        clean.pop("_meta", None)
        clean.pop("_mid_turn", None)
        clean.pop(_RUNTIME_KEY, None)
        return clean

    def _head_for_turn(self, prompt: str) -> str:
        """The head to send: held where nobody asked, rebuilt where they did.

        The whole head is frozen for the life of a conversation. It was once,
        then was not, and the list of what that has to satisfy is worth keeping
        in one place because the next change to this has to satisfy all of it
        at once:

        1. An edit to SOUL.md, USER.md or OPERATING.md reaches the ACTIVE
           session. That is what `prompt_builder` is FOR, and
           `fix_pass_2026_04_24/test_prompt_rebuild.py` exists because the
           head "was previously frozen until next session".
        2. A directive edit reaches the next turn. `prompt.py` promises the
           operator that "an edit is the only thing that moves them".
        3. `project_open` reaches the next turn. An assistant still naming
           the old project after being asked to switch is wrong in a way no
           cache saving pays for.
        4. The clock and the autonomy digest never enter the head at all.
        5. The head is a cache prefix, so any byte that moves in it re-reads
           the entire conversation behind it at full price.
        6. What moves in the head with nobody asking is the memory capsule
           and the diary digest, and nothing the assistant loses by holding
           them is out of reach: `memory_search` fetches anything newer.
        7. A new conversation reads them fresh, and so does a fold, which
           has thrown the cached prefix away regardless.

        The mistake the middle version made was reading 1 to 3 as "rebuild the
        head every turn, in case". That pays on every turn for an act that
        happens on almost none of them, and it left five more blocks that move
        for nobody's reason sitting in the prefix unheld: `changes="never"` was
        never enforced, and every one of those sections re-reads its file on
        each assembly.

        1 is answered by `prompt.bump_head_revision()`, called where the
        operator's act lands rather than guessed at from the bytes, which
        cannot tell an approved edit from a background job rewriting the same
        file. It is a counter on disk, because the agent controller is a
        separate process and a module global would never reach it.

        2 is answered by NOT holding the directives at all. Nothing at a
        memory write retires a hold, so holding them swallowed exactly the
        edits this list promises to deliver. A section may be held only if it
        can name what retires it (`prompt.Section.hold_reason`), and the
        rebuild it falls back to is free while the bytes do not move.

        3 is answered by taking the project block OUT of the head
        (`Section.rides_late`), because that is the one act that must reach the
        turn after it rather than the next conversation. 4 is the split this
        method never sees. 5 is why holding is worth anything. 6 is what
        makes it safe.

        7 is two things, because a `ChatSession` outlives a conversation.
        `refresh_head` covers the boundaries the session knows it crossed:
        `reset`, and a fold, released where the fold actually rewrites the
        history and not on the two exits where it does not. `_conversation_id`
        covers the one it does not — a session handed another conversation's
        history while it goes on living, which `commands.py`'s batch
        compaction does — so a snapshot taken for one chat is never spent on
        another.

        A `prompt` with no sections on it (a plain string, or a caller that
        built `PromptParts` from two halves) is sent whole and held not at
        all, which is the behaviour before any of this existed. A caller that
        APPENDED to the head keeps what it appended: the controller adds a
        seat constraint that way, and re-joining the sections without it would
        drop a HARD RULE on the floor to save some tokens.
        """
        # Local, like every other `tesseract.brain` import in this file:
        # `prompt` reaches the workspace and the memory store at import time
        # and nothing here needs it until a turn is being built.
        from tesseract.brain import prompt as prompt_module

        head = getattr(prompt, "head", prompt)
        sections = getattr(prompt, "sections", None)
        # Rebuild only the part the sections account for, and carry anything a
        # caller put after it through untouched. A head that is not its own
        # sections joined is one this cannot safely rewrite, so it is sent
        # exactly as it arrived and nothing is held or recorded: no saving, no
        # lost text. The guard is checked BEFORE the snapshot, so what is
        # recorded is only ever bytes that were also sent.
        as_built = (
            prompt_module.join_sections(sections, volatile=False)
            if sections else ""
        )
        if sections and head.startswith(as_built):
            here = self._conversation_id()
            revision = prompt_module.head_revision()
            if (
                self._held_sections is None
                or self._held_for != here
                or self._held_revision != revision
            ):
                self._held_sections = {
                    name: sections[name]
                    for name in prompt_module.HELD_SECTION_NAMES
                    if name in sections
                }
                self._held_for = here
                self._held_revision = revision
            merged = dict(sections)
            for name in prompt_module.HELD_SECTION_NAMES:
                if name in self._held_sections:
                    merged[name] = self._held_sections[name]
                else:
                    merged.pop(name, None)
            head = prompt_module.join_sections(
                merged, volatile=False
            ) + head[len(as_built):]
        return self._watch_head(head)

    def _conversation_id(self) -> str:
        """Which conversation this session is currently carrying.

        `tool_context.chat_id` when there is one: `stamp_chat_id` sets it at
        every registration point, the Telegram bridge sets it from the durable
        id, and it is the same string for the life of one chat and different
        in every other. That is what the hold needs.

        `_conversation_tag` is the fallback, and only that. It hashes the
        first message, so two chats that open with the same words share it,
        which is fine for reading a log back and is not an identity. Nothing
        unstamped shares a `ChatSession` with anything else today, so the
        fallback is reached by test doubles and by nothing in production.
        """
        chat_id = getattr(self.tool_context, "chat_id", "")
        return chat_id or self._conversation_tag(self.history)

    def _watch_head(self, head: str) -> str:
        """Count what the hold did not cover, and send it anyway.

        The hold covers five sections, not the whole head, so this counts two
        different things and it is worth knowing which:

        - an operator act retiring the hold through `prompt.bump_head_revision`,
          which is what the three inlined documents move on;
        - any change to a section that rides in the head UNHELD — the tool map,
          the saved directives, the pointer list, the channel overlay. Those
          are rebuilt every turn by design, so a directive saved or a skill
          written moves the head with no revision bump anywhere.

        Both are bills rather than defects. What would be a defect is a number
        that climbs while nothing was edited, no directive was saved and no
        tool was promoted: that means a write path is retiring holds for a
        background write, and `workspace_changes.apply_change` is where that
        is fixed.
        """
        if self._last_head is None:
            self._last_head = head
            return head
        if head != self._last_head:
            self._head_drift += 1
            logger.info(
                "prompt head: moved %+d chars (%d time(s) this conversation); "
                "every move re-reads the whole prompt",
                len(head) - len(self._last_head), self._head_drift,
            )
            self._last_head = head
        return head

    def note_continuity(self, text: str) -> bool:
        """Put the continuity package in front of this conversation.

        Written as a `user` message because that is the only role a provider
        lets a caller place mid-conversation, and marked so the transcript
        draws it as the runtime rather than putting the operator's name on
        sentences they never typed. It does not open a turn: nobody said
        anything, and counting it would make a cleared conversation report a
        turn it never took.

        Appended, not prepended. A boundary clears the history and this runs
        moments later, so in the ordinary case it IS the first message; if the
        operator got there first it sits after their question, which is late
        rather than wrong, and putting it in front of a message already
        answered would rewrite what the turn saw.
        """
        body = (text or "").strip()
        if not body:
            return False
        self.history.append({
            "role": "user",
            "content": body,
            "timestamp": _now_iso(),
            _RUNTIME_KEY: RUNTIME_CONTINUITY,
        })
        return True

    def refresh_head(self) -> None:
        """Drop what this session carried for a conversation that has been
        rewritten: the held sections, and the last fullness reading.

        Called where the cached prefix is gone anyway, which is the only
        moment a fresh capsule costs nothing. Both things it drops answer the
        same question — this session's copy of a conversation was replaced —
        and they are dropped together because the four sites that replace one
        are the four sites that replace the other. Kept apart, `/compact_file`
        on a chat not in focus rewrote that chat's history through
        `commands.py` rather than through `compact()`, so the head was
        refreshed and the reading was not, and the next turn on it reported
        a room the fold had just made back.
        """
        self._held_sections = None
        self._held_for = None
        self._held_revision = None
        context_signal.forget(self._failures_scope_id)

    @property
    def head_hold(self) -> tuple[dict[str, str] | None, str | None, int | None]:
        """The hold, for a caller that is about to borrow this session.

        `commands.py` compacts a chat that is NOT the one in focus by putting
        that chat's history onto the live session, folding, and putting the
        live history back. The fold releases the hold, and the hold belongs to
        the conversation whose history was moved aside — so the chat in focus
        would pay a full re-read because a different chat was compacted from
        the list.

        `_conversation_id` cannot tell those apart: the session is still
        stamped with the chat in focus while it carries the other one's
        history. Only the caller knows, and it already saves and restores the
        history, so it restores this the same way.
        """
        return self._held_sections, self._held_for, self._held_revision

    @head_hold.setter
    def head_hold(
        self, hold: tuple[dict[str, str] | None, str | None, int | None]
    ) -> None:
        self._held_sections, self._held_for, self._held_revision = hold

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

    def _grown_mid_turn(self) -> bool:
        """A cheap "is it worth asking properly" for the tool loop.

        `should_compact` assembles the whole payload and rebuilds the system
        prompt, which is right once per turn and wrong eighty times inside
        one. This sums what history holds, which is what a fold can actually
        remove, and it only ever decides whether to ASK: the real decision is
        still `should_compact`'s, reached through the caller's hook.

        It can be short by the size of this turn's injections, which ride on
        the assembled user message rather than in history. That is a few
        thousand characters against a trigger of hundreds of thousands, and the
        turn boundary catches what it misses.

        It asks the SAME question `should_compact` asks, in the same unit, off
        a cheaper measurement: characters through the runtime's one measured
        divisor. Asking a different question here would be a third trigger, and
        two was already one too many.
        """
        chars = sum(
            _content_chars(m)
            for m in self.history
            if not _is_late_prompt_message(m)
        )
        ctx = self.options.context_window or 0
        if ctx <= 0:
            return False
        return tokens_from_chars(chars) >= self._fold_trigger_tokens(
            ctx, self._system_tokens,
        )

    def _messages_for_turn(self) -> list[dict[str, Any]]:
        """The assembled payload, with the emergency guard applied.

        What goes to the adapter. `_assemble_for_turn` is what the payload IS;
        this is what survives the char ceiling. Anything asking how big the
        conversation has GROWN must ask the assembly, not this — see
        `token_estimate`.
        """
        msgs, protected = self._assemble_for_turn()
        if sum(_content_chars(m) for m in msgs) > self.prompt_char_budget:
            self._guard_firings_this_turn += 1
            self._guard_firings_this_session += 1
            if self._guard_firings_this_turn == _GUARD_LOOP_FAULT_AT:
                # A fault, and `log.error` is what makes it one: `logsetup`
                # turns a backend ERROR into an AR-15 envelope row. The guard
                # firing twice inside a turn means compaction is not bounding
                # the conversation and the guard is doing it instead, one
                # cleared tool result at a time. It fired seventeen times in
                # two minutes before anything said so. Once per turn, at the
                # second firing: the sixteen after it are the same fault.
                logger.error(
                    "the prompt guard has fired %d times inside one turn — "
                    "compaction is not bounding this conversation and the "
                    "guard is clearing tool results to keep the turn alive",
                    self._guard_firings_this_turn,
                )
        return _trim_to_budget(
            msgs, char_limit=self.prompt_char_budget, protected=protected,
        )

    def _assemble_for_turn(self) -> tuple[list[dict[str, Any]], frozenset[int]]:
        """Build the turn's message list. No ceiling, no trimming.

        Split out from `_messages_for_turn` because `token_estimate` read that
        method, which trimmed, so the estimate measured the guard's OUTPUT.
        Once the guard engaged, the estimate read ~30k tokens no matter how
        large the conversation actually was, `should_compact` compared that to
        280k and said no, and compaction became permanently unreachable. The
        trim hid the growth that was supposed to trigger the fix. Measured on
        a live Telegram session, 2026-08-23.
        """
        msgs: list[dict[str, Any]] = []
        # Built once per turn, not once per tool-loop iteration. The volatile
        # half reads a clock and a memory tree, so rebuilding it mid-loop
        # changes bytes the previous iteration already paid to have cached,
        # and the turn re-processes its own tool traffic to no purpose. The
        # approved classification is per TURN — the operator changes a
        # directive expecting the next turn to obey — so a turn holding one
        # reading of its own state is what was agreed, not a compromise of it.
        prompt = self._turn_prompt
        if prompt is None:
            prompt = self._current_system_prompt()
            if self._turn_active:
                self._turn_prompt = prompt
        # `PromptParts` says where the prompt splits. The head is everything
        # whose bytes hold still between turns — the documents, the tool map,
        # and the retrieved memory, held or rebuilt by `_head_for_turn`
        # according to whether the operator or a background job moves it. The
        # late half is only what differs on every single turn: the clock and
        # the autonomy digest. A plain string means nothing said where to
        # split it, so all of it goes in the system message, which is what
        # happened before the split existed.
        late = getattr(prompt, "late", "")
        head = self._head_for_turn(prompt)
        if head:
            msgs.append({"role": "system", "content": head})
        # The per-turn blocks go AFTER everything, past the conversation and
        # past the operator's own message.
        #
        # Caching is a prefix match, so the only thing that decides how much
        # of a request can be reused is how far down the first difference
        # sits. A block whose bytes change every turn puts that difference
        # wherever it is placed, and everything below it is re-read at full
        # price. Placed above the operator's turn — where these rode until
        # this was measured — the difference lands one turn deeper each turn,
        # so no request ever reused more than the system message and the tool
        # schemas, however long the conversation had grown. Placed last, the
        # prefix is the head plus the whole conversation, it only ever grows,
        # and what is re-read is these few hundred tokens.
        #
        # It costs the tool loop the same few hundred tokens per iteration,
        # because iteration 1 has its tool traffic where iteration 0 had this
        # block. That is the trade, and it is worth taking at this size; it
        # would not have been while the memory capsule was still in here.
        #
        # After the operator's question rather than before it: this is not an
        # instruction, it is state, and it says so in `_LATE_PROMPT_LEAD`.
        # Appending is also legal after anything — a tool call and its result
        # can no longer be split by it, which is what the anchor index and its
        # three staleness guards existed to prevent.
        #
        # The late half stays ABOVE the injection, which is cleared after
        # iteration 0. Both are in the tail either way; the order is kept so
        # the assistant reads them in the order it always has.
        trailing: list[dict[str, Any]] = []
        if late:
            trailing.append({
                "role": "user",
                "content": _LATE_PROMPT_LEAD + late,
                _RUNTIME_KEY: _RUNTIME_LATE_PROMPT,
            })
        if self._turn_injection:
            trailing.append({"role": "user", "content": self._turn_injection})
        msgs.extend(self._adapter_message(m) for m in self.history)
        # Say where the stable part ends, for the adapters whose provider only
        # caches where it is told. The last message of the conversation is it:
        # everything above grows and never rewrites, everything below is this
        # turn's own state. Copied rather than stamped in place, because
        # `_adapter_message` hands back the history entry itself when it has
        # no sidecars to strip, and marking that would write into `history`.
        if self.history:
            msgs[-1] = {**msgs[-1], CACHE_BOUNDARY: True}
        # With no history at all there is no turn for the state to be current
        # as of, so it is dropped — and there is no conversation to mark the
        # end of either. The system prompt carries its own breakpoint.
        protected = (
            self._append_protected(msgs, trailing) if self.history else frozenset()
        )
        return msgs, protected

    @staticmethod
    def _append_protected(
        msgs: list[dict[str, Any]], trailing: list[dict[str, Any]],
    ) -> frozenset[int]:
        """Append the runtime blocks and report where they landed, so the
        budget trim can keep them without recomputing the offset itself."""
        at = range(len(msgs), len(msgs) + len(trailing))
        msgs.extend(trailing)
        return frozenset(at)

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
        The drain is pinned to the queued payload, so a successful reply on
        turn A cannot mark unrelated items B, C delivered before their own
        queued turns execute.

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
            and self._pending_nudge is None
            and not self._pending_conscience
            and not self._pending_spawn_completions
            and not self._pending_card_presses
            and not workspace_comment_blocks
            and not workspace_post_blocks
        ):
            return ""
        blocks: list[str] = []
        # First, because a boundary outranks housekeeping: it is the one
        # signal that might end the conversation the rest of them are about.
        if self._pending_nudge is not None:
            blocks.append(format_for_injection(self._pending_nudge))
            self._delivered_nudge = self._pending_nudge
            self._pending_nudge = None
        while self._pending_suggestions:
            blocks.append(format_for_injection(self._pending_suggestions.popleft()))
        while self._pending_conscience:
            blocks.append(self._pending_conscience.popleft())
        while self._pending_card_presses:
            blocks.append(self._pending_card_presses.popleft())
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
            enabled_extended=self._enabled_extended_tools,
            defer_outside_working_set=getattr(
                self.adapter, "defers_tool_loading", False
            ),
        )

    async def send(
        self,
        user_text: str | list[dict[str, Any]],
        *,
        transient: bool = False,
        workspace_origin: dict[str, str] | None = None,
        view_snapshot: dict[str, Any] | None = None,
        runtime_origin: str | None = None,
        fold_when_needed: Callable[[], Awaitable[None]] | None = None,
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

        ``runtime_origin`` (one of :data:`RUNTIME_ORIGINS`) says the runtime
        wrote this turn's text, not the operator. It is stamped on the stored
        message and stripped before the request goes out, so the transcript
        can draw it as the runtime speaking and a chat's name is never taken
        from a sentence nobody typed. An unknown value raises: the mark is
        what a surface renders, and a name nothing can draw is worse than the
        bug it was meant to fix.
        """
        if runtime_origin is not None and runtime_origin not in RUNTIME_ORIGINS:
            raise ValueError(
                f"unknown runtime_origin {runtime_origin!r}; "
                f"expected one of {sorted(RUNTIME_ORIGINS)}"
            )
        # The turn's own record opens before anything can refuse it, because a
        # turn that never began is one of the things "did it work" has to be
        # able to answer. Synthetic workspace turns are excluded for the same
        # reason they are kept out of history: they are rolled back at the end
        # and a record of one describes work that no longer exists.
        if self.turn_entry and not transient:
            self._turn_recorder = TurnRecorder(
                entry=self._turn_record_entry,
                session_id=self.tool_context.session_id,
            )
            # Bound into `orchestrator.turns.context` so a record written under
            # the turn (a cost row) can name it; released where it is closed.
            enter_turn(self._turn_recorder)
            self.last_turn_id = self._turn_recorder.turn_id
            self._beat("getting ready")
        else:
            self.last_turn_id = ""
        turn_outcome = RunOutcome.FAILED
        turn_reason = "the turn ended without recording how"
        # Everything from here to the streaming `try` runs with the turn's
        # record open and its binding live, and none of it was guarded: a
        # raise in preflight or recall left the record open on disk and the
        # binding pointing at a dead turn for the next write in this task to
        # read. Closed on the way out, the way the streaming `finally` does.
        try:
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
                        # Refused, not failed: the turn deliberately did not begin.
                        # This exit is above the `try` that owns the finally below,
                        # so it closes its own record.
                        self.last_turn_outcome = RunOutcome.REFUSED
                        self.last_turn_reason = str(exc)
                        self._close_turn_record(RunOutcome.REFUSED, str(exc))
                        yield StreamChunk(
                            type=ChunkType.ERROR,
                            # The sentence, not `str(exc)`. The exception's own text
                            # is machine text and it reached a phone verbatim, where
                            # the leak filter ate it for containing a slash. What a
                            # person needs is the cause and the remedy.
                            error=exc.as_sentence(),
                            raw={
                                "severity": "warning",
                                "reason": "budget_exhausted",
                                "detail": str(exc),
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
            self._tool_failure_limit_cache = None
            self._guard_firings_this_turn = 0
            # A turn that died before the boundary must not hand its decision
            # to the next one: the conversation it wanted to leave behind is
            # not the conversation that would be folded.
            self._continuation = ""
            user_message: dict[str, Any] = {
                "role": "user",
                "content": user_text,
                "timestamp": _now_iso(),
            }
            if runtime_origin is not None:
                user_message[_RUNTIME_KEY] = runtime_origin
            self.history.append(user_message)
            appended_user_idx = len(self.history) - 1
            self._turn_active = True
            self._turn_prompt = None
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
                # What the map says these records connect to, on the push that
                # already happens rather than as a second one. `with_connections`
                # carries the whole argument for why; in short, the atlas was
                # scoped to a question nobody types out loud.
                recall_items = with_connections(
                    recall_items, limit=recall_cfg.connections_per_memory
                )
                recall_block = format_recall_block(recall_items)
                if recall_block:
                    self._turn_injection = (
                        f"{recall_block}\n\n{self._turn_injection}"
                        if self._turn_injection
                        else recall_block
                    )

            # Ambient observer: prepend `current_view` + `view_state`
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

        except BaseException as exc:
            # The refused path above closes its own record and then yields, so
            # a consumer that lets go at that yield arrives here with nothing
            # open; its REFUSED must not be rewritten as a failure.
            if self._turn_recorder is not None:
                self.last_turn_outcome = RunOutcome.FAILED
                self.last_turn_reason = f"{type(exc).__name__}: {exc}"
                self._close_turn_record(RunOutcome.FAILED, self.last_turn_reason)
            raise

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

                # Pause, fold, continue. Compaction used to happen only
                # BETWEEN turns, so one turn could grow past the ceiling on its
                # own: the cap allows eighty tool calls, and a loop dumping
                # large results crosses a budget built for a conversation. What
                # caught it was the emergency guard, which fired seventeen
                # times in two minutes on 2026-08-24 clearing re-fetchable tool
                # results while the same turn added more.
                #
                # `fold_when_needed` is the caller's own after-turn hook,
                # handed in rather than reimplemented, so the decision, the
                # tally, the log entry and the notice are the ones both funnels
                # already share. Not on iteration 0: the turn boundary just
                # ran, and folding twice in a row buys nothing.
                if iteration and fold_when_needed is not None and self._grown_mid_turn():
                    await fold_when_needed()
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
                _step_started = datetime.now(timezone.utc)
                _schemas = self._tool_schemas()
                # Recorded from what this iteration is ABOUT to send, so the
                # cache line beside the usage costs a tuple rather than a
                # second assembly. Rebuilding it there doubled the prompt
                # assembly and the schema build on every round trip, which is
                # the cost the timing three lines above exists to watch.
                self._last_request_shape = self._request_shape(messages, _schemas)
                self._beat("waiting for the model to answer")
                try:
                    async for chunk in self.adapter.stream(
                        messages=messages,
                        tools=_schemas,
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
                                turn_outcome = RunOutcome.REFUSED
                                turn_reason = chunk.error or "the turn was stopped deliberately"
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

                # One step per roundtrip to the provider. A cut-off stream
                # commits nothing on purpose: the last committed step is where
                # the turn got to, and a call that never returned did not.
                self._record_step(
                    kind="model",
                    name=self.options.role or "chat_brain",
                    outcome=(
                        RunOutcome.FAILED if adapter_error_seen
                        else RunOutcome.TRUNCATED if _is_truncated_stop(stop_reason)
                        else RunOutcome.SUCCEEDED
                    ),
                    reason=(
                        adapter_error_text if adapter_error_seen
                        else f"the provider cut the reply off ({stop_reason})"
                        if _is_truncated_stop(stop_reason)
                        else ""
                    ),
                    started_at=_step_started,
                )

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
                        turn_outcome = RunOutcome.FAILED
                        turn_reason = (
                            f"the model failed {self.max_consecutive_adapter_errors} "
                            f"times in a row and the breaker opened. "
                            f"Last: {adapter_error_text}"
                        )
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
                    # A turn that said nothing, or said half of something, ran
                    # to the end and still did not deliver what a turn owes.
                    # `degraded` is that distinction, and it is the reason the
                    # vocabulary has a word between succeeded and failed.
                    if status_only:
                        turn_outcome = RunOutcome.DEGRADED
                        turn_reason = "the turn ended with no reply, twice in a row"
                    elif truncated:
                        turn_outcome = RunOutcome.DEGRADED
                        turn_reason = f"the reply was cut off ({stop_reason})"
                    else:
                        turn_outcome = RunOutcome.SUCCEEDED
                        turn_reason = ""
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

                # Surface any background spawns that completed
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
        except asyncio.CancelledError:
            # The 2026-08-24 incident's own shape: a shutdown drain cancels a
            # turn that is minutes into its work. The record says so rather
            # than the turn simply ceasing to exist.
            turn_outcome = RunOutcome.TRUNCATED
            turn_reason = "the turn was cancelled before it finished"
            raise
        except GeneratorExit:
            # Whoever was reading this stream stopped reading it: a closed
            # socket, a channel that hung up. Same shape as a cancel, and
            # naming it separately is what tells the two apart afterwards.
            turn_outcome = RunOutcome.TRUNCATED
            turn_reason = "whoever was waiting stopped listening before the turn finished"
            raise
        except Exception as exc:
            # Re-raised untouched. Catching it only buys the record a reason,
            # and a turn whose record says `failed` with no cause is the thing
            # this phase exists to stop.
            turn_outcome = RunOutcome.FAILED
            turn_reason = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.last_turn_outcome = turn_outcome
            self.last_turn_reason = turn_reason
            self._close_turn_record(turn_outcome, turn_reason)
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
                # Leaf stream — emit one MemoryLeaf per completed
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
            self._turn_active = False
            self._turn_prompt = None
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
        billed_options = getattr(self.adapter, "last_used_options", None) or self.options
        raw = stop_chunk.raw or {}
        usage = raw.get("usage") if isinstance(raw, dict) else None
        if not isinstance(usage, dict):
            return
        if self._bill(usage, billed_options):
            self._log_cache_shape(usage)

    def _bill(self, usage: dict[str, Any], options: AdapterOptions) -> bool:
        """Put one provider call on the ledger. True when a row was written.

        The one seam. Compaction is the reason it is named: `compact_history`
        ran a model call and threw its STOP chunk away, so the largest single
        input a session ever sends billed nothing at all, on a chain that is
        not `MeteredAdapter`-wrapped and therefore had no second net under it.
        A call that reaches a provider and not this method is spend nobody can
        see.
        """
        if self.cost_ledger is None or not options.model:
            return False
        try:
            self.cost_ledger.record(
                options.role or "chat_brain",
                options.model,
                CostUsage.from_raw(usage),
                tier=options.tier or "",
            )
        except RuntimeError:
            # Missing or unpriceable catalog entry — logged inside the ledger.
            # The turn itself must not die because the accountant cannot price
            # it.
            logger.exception("cost ledger record failed")
            return False
        return True

    def _log_cache_shape(self, usage: dict[str, Any]) -> None:
        """One line per provider call: what was cached, and what the request
        was SHAPED like when it was.

        This line answered the question it was added for. Roughly half the
        input of a measured session was being paid for at full price, and two
        shapes accounted for most of it:

        - `tools` changed. Every `cached=0` line was a tool-count change, with
          no exceptions, which is what proved the render order: tools go
          before the system prompt, so adding one invalidates the head as well
          as the conversation. `tool_search` unlocks a tool for the NEXT
          ITERATION, so several landed mid-turn and re-read the whole prompt
          seconds later.
        - `late_at` moved. The per-turn block sat above the operator's
          message, so each turn put the first difference one turn deeper and
          the cache fell to a floor that was exactly the head plus the tools,
          confirmed against four different tool counts. That is what moved it
          below everything.

        Two candidates were eliminated by the same data rather than by
        reading: the emergency prompt guard never fired in the measured
        window, and no gap between calls came near a cache TTL.

        The fields still watch for both. `late_at` should now sit at the very
        end of the list and `msgs` should only grow; `tools` should hold still
        for the life of a session; `sys_chars` says whether the head itself
        changed, which after the tiering means a memory was written.
        """
        shape = self._last_request_shape
        if shape is None:
            return
        logger.info(
            "cache shape: cached=%s in=%s conv=%s msgs=%d late_at=%d tools=%d sys_chars=%d",
            usage.get("cached_tokens"),
            usage.get("input_tokens"),
            *shape,
        )

    @staticmethod
    def _conversation_tag(messages: list[dict[str, Any]]) -> str:
        """Which conversation this call belongs to, in eight hex characters.

        The earliest message that is not the system prompt, keyed-hashed.
        Fixed for the life of a conversation, different in every one, and
        already in the request.

        KEYED, with a secret made once per process and never written down. A
        bare digest of operator text is not the same thing as no operator text:
        eight hex characters is 32 bits, and a reader of these logs who can
        guess a short opening line can confirm it by hashing candidates. The
        adapter beside this one states the rule these logs are held to —
        "hashes, never content" — and an unkeyed hash of content does not meet
        it. The key costs nothing and the tag is only ever compared with
        itself.

        Without it these lines cannot be read at all once more than one
        conversation is live, and one always is: the cockpit, a channel turn,
        the observer and the panel writer all land in the same log. Every
        analysis in this investigation that compared a call to the one before
        it was wrong for that reason, three times, each time inventing a cache
        miss that had not happened.
        """
        for msg in messages:
            if msg.get("role") == "system":
                continue
            content = msg.get("content")
            if not isinstance(content, str):
                content = json.dumps(content, sort_keys=True, default=str)
            if content:
                return hmac.new(
                    _LOG_TAG_KEY, content.encode("utf-8"), hashlib.sha256
                ).hexdigest()[:8]
        return "-"

    @staticmethod
    def _request_shape(
        messages: list[dict[str, Any]], schemas: list[dict[str, Any]] | None
    ) -> tuple[str, int, int, int, int]:
        """`(conversation, messages, index of the per-turn block, tools, system chars)`.

        Read off the request that is about to go out. Each one is a thing that
        can shorten the cached prefix, and the line they end up on tells them
        apart: the conversation says WHICH prefix this call was meant to reuse,
        the index says where the per-turn block sat,
        the tool count says whether `tool_search` grew the array mid-session,
        and the system length says whether the head itself changed.
        """
        late_at = next(
            (
                i
                for i, m in enumerate(messages)
                if m.get(_RUNTIME_KEY) == _RUNTIME_LATE_PROMPT
            ),
            -1,
        )
        head = messages[0] if messages and messages[0].get("role") == "system" else None
        return (
            ChatSession._conversation_tag(messages),
            len(messages),
            late_at,
            len(schemas or ()),
            len(head.get("content") or "") if head else 0,
        )

    def _notify_observer_turn_end(self) -> None:
        sub = self._observer_subscriber
        # The watermark advances whether or not the observer is armed. A
        # session attaches when it is BUILT, so a disarmed stretch would
        # otherwise bank its turns and flood the first pass after re-arming
        # with a conversation the operator never asked it to read.
        start = self._observer_last_index
        self._observer_last_index = len(self.history)
        if sub is None or not getattr(sub, "is_active", False):
            return
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
            sub.on_loop_end(new_turns, self)
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
        the leaf stream. Source is derived from ``session_kind``
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

        # Partition by concurrency safety. Tools
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
                self.tool_context,
                current_call_id=tc.id,
                turn_id=self._turn_recorder.turn_id if self._turn_recorder else "",
                bind_task=self._turn_recorder.bind_task if self._turn_recorder else None,
            )
            result = await execute_tool(
                registry=self.registry,
                tool_name=tc.name,
                tool_input=tc.input,
                context=per_call_ctx,
                ask_fn=self.ask_fn,
                policy=self.policy,
            )
            # Every result passes through here, so it is also where the one
            # thing the frozen head cannot survive is caught. Only on success:
            # a refused or failed `project_open` moved nothing, and throwing
            # the prefix away for it would pay the whole cost of a switch that
            # did not happen.
            # Every result passes through here, so this is where a result too
            # large to send is caught. Read at call time, like every other
            # runtime limit, so an operator edit applies without a restart.
            return idx, tc, bound_tool_result(
                result,
                tc.name,
                self.options.context_window,
                _result_window_share(),
            )

        safe_indices: list[int] = []
        unsafe_indices: list[int] = []
        for i, tc in enumerate(pending_calls):
            tool = self.registry.get(tc.name)
            if tool is not None and tool.is_concurrency_safe():
                safe_indices.append(i)
            else:
                unsafe_indices.append(i)

        # The last beat before the turn disappears into the tools. A call that
        # hangs stops the clock here, which is what lets the panel say the turn
        # is held on `weather_lookup` rather than that it is busy.
        self._beat(
            ("running " + ", ".join(dict.fromkeys(tc.name for tc in pending_calls)))
            if pending_calls
            else "running a tool"
        )

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
            # Every tool result the turn produces passes through here, which is
            # what makes the manifest reconcilable against the transcript: a
            # call with no row here is a call that never returned one.
            if result.denied_hard:
                outcome = RunOutcome.REFUSED
                reason = result.deny_reason or "the permission gate refused it"
            elif result.timed_out:
                outcome = RunOutcome.TRUNCATED
                reason = "it ran out of time before it finished"
            elif result.is_error:
                outcome = RunOutcome.FAILED
                reason = result.output[:_STEP_REASON_CHARS]
            else:
                outcome = RunOutcome.SUCCEEDED
                reason = ""
            self._record_step(kind="tool", name=tc.name, outcome=outcome, reason=reason)
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
                # Escalate-on-failure reflex, signal side.
                # Consecutive-in-order failures of the SAME tool name
                # within this turn: at `roles.yaml::boundary
                # .tool_failure_limit`, surface `(name, count)` via
                # failures_signal so the digest carries an "escalate now"
                # line (rule 11-error-recovery.md reads it), and so
                # `after_turn` takes a boundary rather than letting the
                # conversation keep trying. A success
                # clears the streak ONLY when it's the tool actually
                # RECORDED in failures_signal — gating on the local tracker
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
                    # One number, from roles.yaml, for both halves of what a
                    # streak means: at the limit it stops being a line in the
                    # digest and becomes a boundary `after_turn` must take.
                    # A hint and a hard stop that disagreed about when the
                    # pattern started would be two answers to one question.
                    #
                    # Read once for the turn, off the hot path. Reading it here
                    # re-parsed roles.yaml on every errored tool result, and a
                    # bad key raised inside the block whose exception fails an
                    # answer that has otherwise already been produced.
                    if self._tool_error_streak_count >= self._tool_failure_limit():
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

    def request_continuation(self, mode: str) -> None:
        """Record what should happen to this conversation once the turn ends.

        Raises `ValueError` on a mode that is not one of `Continuation`'s, so a
        caller that misspells it hears about it instead of having its decision
        silently dropped at the boundary.

        Last call wins. A turn that asks twice has changed its mind, and
        carrying both would mean folding a conversation on its way out.
        """
        self._continuation = Continuation(mode).value

    def take_continuation(self) -> str:
        """What this turn asked for, cleared as it is read.

        Cleared here rather than by the reader, because the boundary acts on it
        once and a mode left behind fires again at the end of the next turn
        against a conversation that never asked.
        """
        mode, self._continuation = self._continuation, ""
        return mode

    def reset(self) -> None:
        self.history.clear()
        self._observer_last_index = 0
        self._pending_suggestions.clear()
        self._pending_conscience.clear()
        self._pending_card_presses.clear()
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
        # Every nudge on this object was about the conversation being wiped.
        # The observer's call runs as a detached task and can land AFTER the
        # boundary that cleared the room, so a pending one here is not merely
        # stale, it is about a conversation the next turn has never seen. The
        # id goes too: dedupe is per conversation, and holding it would silence
        # a genuine repeat in the fresh one.
        self._pending_nudge = None
        self._delivered_nudge = None
        self._last_nudge_observation_id = None
        # The observer's rolling window is per conversation and was the one
        # piece of its state that survived the wipe: the next observation
        # would have read the cleared conversation's turns alongside the new
        # one's, in the same prompt.
        self._observer_transcript.reset()
        # Anything still in flight against the conversation that just ended
        # now describes nothing. It checks this on the way back.
        self._conversation_generation += 1
        self._turn_injection = ""
        self._turn_active = False
        self._turn_prompt = None
        # A wiped history is a new conversation on the same object, and the
        # held sections are held for the life of a CONVERSATION. Nothing is
        # cached in front of an empty history either, so reading them again is
        # free here for the same reason it is free after a fold.
        self.refresh_head()
        self._consecutive_adapter_errors = 0
        self._tool_error_streak_name = ""
        self._tool_error_streak_count = 0
        self._pending_workspace_comment_ids = []
        self._pending_workspace_event_ids = []
        self.pending_injected_messages = []
        self.tool_context.todos.clear()
        # Cancel any background spawns. cancel_all is async
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
        """How big the conversation IS, not how big the guard let it look.

        Reads the untrimmed assembly. Reading `_messages_for_turn` meant this
        measured the guard's output, and a guarded session reported the same
        small number every turn forever.
        """
        return self.adapter.count_tokens(self._assemble_for_turn()[0])

    def should_compact(self) -> bool:
        """True when history has grown past compact_threshold of context window.

        We gate on a minimum history length so a fresh session doesn't
        compact prematurely, and on there being something between the head
        anchor and the verbatim tail for a fold to actually fold. Saying yes
        with an empty middle is not free: the turn boundary spends a
        reflection call, a real model turn, before `compact` discovers there
        was nothing to do.

One question, in tokens, and it is `compact_ratio`'s. It used to be
        two: a char arm derived from the guard's budget was asked first and
        answered yes first on every real configuration, so the dial the
        operator turns decided nothing. The header records what that cost and
        why the char arm is not needed to hold the guarantee it was added for.
        """
        ctx = self.options.context_window or 0
        if ctx <= 0:
            return False
        # Messages, not turns: a conservative minimum below which folding
        # cannot be worth a model call whatever the turn count says.
        if len(self.history) <= self.head_anchor_messages + 6:
            return False
        # Assembled once and asked both questions. Assembly walks the whole
        # history, and this runs after every turn on both surfaces.
        # Measured here and kept, because the trigger subtracts it and because
        # everything that reports it was counting the frozen `system_prompt`
        # instead of the message this turn assembled.
        foldable, self._system_tokens = self._foldable_split(
            self._assemble_for_turn()[0]
        )
        foldable_tokens = self.adapter.count_tokens(foldable)
        trigger = self._fold_trigger_tokens(ctx, self._system_tokens)
        # Published on the way past, from the numbers the decision already
        # compared. The next turn's prompt renders it, so the model reaches a
        # boundary knowing how close it is instead of having to ask.
        context_signal.record(
            self._failures_scope_id,
            foldable_tokens=foldable_tokens,
            trigger_tokens=trigger,
        )
        if foldable_tokens < trigger:
            return False
        # An arm said yes. Whether that is worth a model call depends on there
        # being something between the anchor and the tail, and finding out
        # walks the turn boundaries, so it is asked last rather than first.
        return not self._middle_is_empty()

    def _foldable_split(
        self, msgs: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], int]:
        """Split an assembled payload into what a fold can remove, and the
        system prompt's size.

        **Pure.** It stamps nothing: `should_compact` owns `_system_tokens`
        because the trigger subtracts it, and a reporting path that overwrote
        it would be mutating the decision's state to draw a bar.

        One definition, because the decision and the report both need it and a
        panel measuring a different slice from the runtime is the defect
        `fold_measurements` exists to close.

        The system message is excluded because compaction cannot shrink it:
        `compact()` rewrites `history` and never touches the head. Counting it
        would mean a head that grew past the trigger on its own made compaction
        fire after every turn, spend a model call, fail to get under, and fire
        again forever.

        The late half of the prompt rides as a transient user message and is
        never written to `history`, so `compact` cannot touch it either.
        Counting it is the system-prompt mistake in another costume: it grows
        on its own (a clock, a memory capsule, the directives), and a payload
        pushed over the line by it folds, fails to get under, and folds again.
        """
        has_system = bool(msgs) and msgs[0].get("role") == "system"
        system_tokens = self.adapter.count_tokens(msgs[:1]) if has_system else 0
        foldable = [
            m
            for m in (msgs[1:] if has_system else msgs)
            if not _is_late_prompt_message(m)
        ]
        return foldable, system_tokens

    def measure_payload(self) -> tuple[int, int, int]:
        """`(total, foldable, system)` tokens, from ONE assembly.

        Every reporter wants all three and the assembly walks the whole
        history, so asking three times is three walks on a path that runs after
        every turn. `context_report.gather` is the caller.
        """
        msgs = self._assemble_for_turn()[0]
        foldable, system_tokens = self._foldable_split(msgs)
        return (
            self.adapter.count_tokens(msgs),
            self.adapter.count_tokens(foldable),
            system_tokens,
        )

    def _middle_is_empty(self) -> bool:
        """True when a fold would find nothing between the anchor and the tail.

        The same partition `compact` makes, asked before the reflection call
        rather than after it. The running summary sits at the anchor's edge
        and `compact` lifts it out before slicing, so it does not count as
        something to fold.
        """
        anchor_end = _find_head_anchor_end(self.history, self.head_anchor_messages)
        first = anchor_end
        if first < len(self.history) and _is_running_summary_message(
            self.history[first]
        ):
            first += 1
        return self._compute_tail_start(self.history, anchor_end) <= first

    def system_prompt_tokens(self) -> int:
        """The system prompt as the fold decision last measured it.

        Every cockpit session carries a `prompt_builder`, so `system_prompt`
        is the string frozen at construction and not what the turn sent. The
        stats that reach a screen have to quote the same number the trigger
        was compared against, or the panel draws a line the runtime does not
        use.
        """
        if self._system_tokens:
            return self._system_tokens
        # Cold: a session restored from disk has history but has not run a
        # turn, so nothing has measured anything yet. Build the prompt rather
        # than counting the frozen string, because the frozen string is what
        # this method exists to stop reporting. One build, and the answer is
        # kept for the next caller.
        built = self._current_system_prompt()
        # The head only, to match what `_assemble_for_turn` puts in the system
        # message. The late half is a separate transient message and counting
        # it here made the cold reading disagree with every warm one.
        prompt = getattr(built, "head", built)
        if not prompt:
            return 0
        # Not stored: `should_compact` writes `_system_tokens` from the message
        # the turn assembled, and keeping this reading would pin a figure taken
        # from a prompt that carries a clock.
        return self.adapter.count_tokens(
            [{"role": "system", "content": str(prompt)}]
        )

    def _fold_trigger_tokens(self, ctx: int, system_tokens: int) -> float:
        """How large the FOLDABLE part may get before a fold is worth running.

        Two numbers, and the larger wins.

        The configured one is `compact_threshold` of the window, less the
        system prompt, because the threshold is a statement about the whole
        payload while only the rest of it can be folded.

        The floor is what a fold always leaves behind — the head anchor and the
        verbatim tail — times `headroom_multiplier`. Below that, compaction
        cannot get under the line it just crossed, so it fires again next turn
        and every turn after, spending a summarisation call each time to
        achieve nothing. `roles.yaml` used to warn about this in a comment; a
        comment cannot check itself, and the configuration it warned about
        shipped anyway.

        The floor raises the trigger and never lowers it, so a conversation
        with room to fold still folds.
        """
        return self._trigger_with_floor(
            ctx, system_tokens, self._unfoldable_tail_tokens(),
        )

    def _trigger_with_floor(
        self, ctx: int, system_tokens: int, unfoldable: int
    ) -> float:
        """The two arms of the trigger, given a floor already measured.

        Split out so `fold_measurements` can report the same number the
        decision uses without measuring the floor a second time.
        """
        return max(
            ctx * self.compact_threshold - system_tokens,
            unfoldable * self.headroom_multiplier,
        )

    def turn_count(self) -> int:
        """How many turns the conversation holds.

        Turns, not messages halved. The two diverge the moment a turn calls a
        tool, which is most of them.
        """
        return len(_turn_starts(self.history))

    def fold_measurements(self, system_tokens: int) -> dict[str, int]:
        """Everything the fold decision rests on, measured in one pass.

        Settings draws the floor the runtime enforces, and it reads these
        rather than recomputing them from the config. A control that derives
        its picture from the setting instead of the measurement is how a
        handle comes to promise something the runtime will not do, which is
        the whole reason the tail stopped being a number somebody typed.
        """
        ctx = self.options.context_window or 0
        anchor_tokens, tail_tokens, tail = self._anchor_and_tail()
        unfoldable = anchor_tokens + tail_tokens
        return {
            "context_window": ctx,
            "system_tokens": system_tokens,
            "head_anchor_tokens": anchor_tokens,
            "tail_tokens": tail_tokens,
            "tail_turns": len(_turn_starts(tail)),
            "keep_recent_turns": self.keep_recent_turns,
            "unfoldable_tokens": unfoldable,
            "fold_trigger_tokens": int(
                self._trigger_with_floor(ctx, system_tokens, unfoldable)
            ),
            # The guard, reported alongside the fold rather than only logged.
            # It trims an assembled prompt back under a char ceiling, and when
            # it does the conversation is being held down by the guard instead
            # of bounded by the dial. That was visible in the backend log and
            # nowhere a person could reach from a phone.
            "guard_char_budget": self.prompt_char_budget,
            "guard_trim_tokens": tokens_from_chars(self.prompt_char_budget),
            "guard_firings": self._guard_firings_this_session,
        }

    def _anchor_and_tail(self) -> tuple[int, int, list[dict[str, Any]]]:
        """The head anchor and the verbatim tail, measured once.

        Both halves are measured from the messages themselves: the head
        anchor, and the turns the tail would keep if a fold ran now. Neither
        is a configured number — the setting says how many turns, and a turn
        has no size until you look at it.

        One function because there are two readers and they must not disagree.
        `_unfoldable_tail_tokens` is what the trigger ENFORCES and
        `fold_measurements` is what Settings REPORTS, and a panel drawing a
        different floor from the one the runtime uses is the whole defect this
        workstream started from.
        """
        anchor_end = _find_head_anchor_end(self.history, self.head_anchor_messages)
        anchor = self.adapter.count_tokens(self.history[:anchor_end])
        tail_start = self._compute_tail_start(self.history, anchor_end, anchor)
        tail = self.history[tail_start:]
        return anchor, self.adapter.count_tokens(tail), tail

    def _unfoldable_tail_tokens(self) -> int:
        """Tokens a fold is guaranteed to keep, beyond the system prompt."""
        anchor, tail_tokens, _ = self._anchor_and_tail()
        return anchor + tail_tokens

    def _tail_ceiling_tokens(self, anchor_tokens: int | None = None) -> int | None:
        """The most the verbatim tail may measure, or None when unknowable.

        Turns are what the operator sets, and a turn has no size: one carrying
        twenty tool calls can outweigh fifty that carry none. So the setting
        says how many turns to keep at most, and this says when to keep fewer.

        The bound is what still leaves compaction able to win. A fold keeps
        the head anchor and the tail, so those two have to sit under the
        trigger with `headroom_multiplier` to spare, or the fold lands back
        over the line it just crossed and fires again next turn.

        **The system prompt is deliberately NOT subtracted here**, though it
        is subtracted from the trigger's configured arm. Taking it off this
        bound too collapses the ceiling to nothing whenever the prompt alone
        exceeds `context_window * compact_threshold`, and a tail trimmed to
        nothing makes the floor tiny, the trigger tiny, and every turn fold.
        That is the failure this whole mechanism exists to prevent. The price
        is that the floor arm can exceed the configured arm by at most the
        size of the prompt, which delays a fold in a tail-heavy session and
        can never push the trigger above `context_window * compact_threshold`.

        `anchor_tokens` is accepted so a caller that has already measured the
        anchor does not pay for it twice on a path that runs every turn.
        """
        ctx = self.options.context_window or 0
        if ctx <= 0:
            return None
        if anchor_tokens is None:
            anchor_end = _find_head_anchor_end(
                self.history, self.head_anchor_messages
            )
            anchor_tokens = self.adapter.count_tokens(self.history[:anchor_end])
        room = ctx * self.compact_threshold / self.headroom_multiplier
        return max(0, int(room) - anchor_tokens)

    async def compact(self) -> tuple[int, int]:
        """Sliding-window compaction with head anchor + append summary.

        Layout after compaction:

            [head_anchor user msgs + interleaving]   # never folded
            [running_summary user msg]               # one msg, grows by append
            [active_tail]                            # token-budgeted

        Returns (tokens_before, tokens_after). No-op (same numbers twice)
        when history is too short or summarization fails.

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
                # Split on the blank line rather than slicing by the current
                # prefix's length: the message may carry an older, shorter
                # prefix, and slicing by today's length would eat the first
                # lines of the summary itself.
                raw = msg.get("content") or ""
                _, sep, body = raw.partition("\n\n")
                prior_summary_text = body.strip() if sep else ""
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
            self._last_fold_outcome = "nothing_to_fold"
            return before, before

        folded = await compact_history(
            self.adapter,
            self.options,
            middle,
            prior_summary=prior_summary_text,
        )
        # Billed before the summary is inspected, and billed even when the
        # summarizer came back empty: a fold that failed sent the same input a
        # fold that worked sends, and charging only for the successful ones is
        # how the expensive failures stayed invisible.
        if folded.usage:
            self._bill(folded.usage, folded.options)
        new_slice_summary = folded.summary
        if not new_slice_summary:
            logger.warning("compaction returned empty summary — keeping full history")
            self._last_fold_outcome = "summarizer_failed"
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
            _RUNTIME_KEY: _RUNTIME_RUNNING_SUMMARY,
        }

        self.history = [
            *head_anchor_msgs,
            running_summary_msg,
            *active_tail,
        ]
        # HERE, and not at the top of this method. The fold has just rewritten
        # the front of the history, so the cached prefix is gone and a fresh
        # capsule costs nothing. Released before the rewrite it would also
        # fire on the two exits above — a no-op fold and a failed summariser,
        # both of which return with the history untouched and the prefix still
        # live. Then a background write would move the head for a fold that
        # never happened, and re-read the whole conversation to pay for it.
        # Drops the held sections and the last fullness reading together: both
        # described the conversation this line has just rewritten.
        self.refresh_head()
        # How many turns the fold left word for word. The transcript draws its
        # divider in front of them, because everything ABOVE the divider is
        # what got summarised and these did not. Without it the divider was
        # appended at the end and labelled the kept turns as folded.
        self._last_fold_tail_turns = len(_turn_starts(active_tail))
        self._last_fold_outcome = "folded"
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
        self,
        history: list[dict[str, Any]],
        lower_bound: int,
        anchor_tokens: int | None = None,
    ) -> int:
        """Return the index from which the verbatim active tail begins.

        The last ``keep_recent_turns`` turns, or fewer when they do not fit
        under :meth:`_tail_ceiling_tokens`. ``lower_bound`` is the earliest
        index the tail may start at, just after the head anchor.

        The most recent turn is kept whatever it measures. Dropping it would
        take away the exchange the next reply answers, and a tail that big is
        the prompt guard's problem rather than something to solve by making
        the assistant forget what was just said.
        """
        starts = _turn_starts(history, lower_bound)
        if not starts:
            return len(history)
        wanted = starts[-max(1, self.keep_recent_turns):]
        tail_start = wanted[-1]
        ceiling = self._tail_ceiling_tokens(anchor_tokens)
        if ceiling is None:
            return max(lower_bound, wanted[0])
        acc = self.adapter.count_tokens(history[tail_start:])
        kept = 1
        for start in reversed(wanted[:-1]):
            turn_tokens = self.adapter.count_tokens(history[start:tail_start])
            if acc + turn_tokens > ceiling:
                break
            acc += turn_tokens
            tail_start = start
            kept += 1
        if kept < len(wanted):
            logger.info(
                "tail: kept %d of %d turns, %d tokens under a %d ceiling",
                kept, len(wanted), acc, ceiling,
            )
        return max(lower_bound, tail_start)
