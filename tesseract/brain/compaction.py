"""Conversation compaction — fold older turns into a structured summary.

When conversation history grows past the model's context window, the
older middle slice is summarized into a single `[Context from earlier]`
message and the tail is kept verbatim. CR-0 (2026-05-22) replaced the
original free-form 4-6 sentence paragraph with a structured 5-section
output and an **append-not-resummarize** contract:

- First compaction → one `# Slice 1` block with five sub-sections.
- Each subsequent compaction → a new `# Slice N` block APPENDED to the
  prior summary verbatim. The model is told it MUST NOT rewrite earlier
  slices. This prevents iterative information loss (4 sentences →
  2 sentences → 1 sentence rot) that the old single-paragraph design
  guaranteed every pass.

Compaction is still a one-shot adapter call with no tools — pure
text-out summarization on the primary adapter (fallbacks are bypassed
so the summarizer's voice stays consistent within a session).
"""

from __future__ import annotations

import logging
from typing import Any

from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ChunkType,
    ModelAdapter,
)

logger = logging.getLogger(__name__)

# Magic prefix that marks the running-summary message. Used by
# `ChatSession.compact` to detect a prior summary and switch into
# append mode.
RUNNING_SUMMARY_PREFIX = "[Context from earlier in this session]"

# Section headers the summarizer must emit. Order is contractual —
# callers that inspect or trim the summary scan in this order.
SECTION_HEADERS: tuple[str, ...] = (
    "## Operator goals",
    "## Decisions made",
    "## Files touched",
    "## Facts learned",
    "## Open threads",
)


_FRESH_SYSTEM_PROMPT = """You are summarizing a conversation slice between the assistant (a personal AI assistant) and the operator (a human builder).

Produce a STRUCTURED summary with EXACTLY these section headers, in this order:

## Operator goals
## Decisions made
## Files touched
## Facts learned
## Open threads

Rules:
- Use the section headers VERBATIM. No other H2 headers.
- Bullet points under each section. Terse — at most 8 bullets per section.
- Plain factual statements; no commentary, no preamble, no closing remarks.
- Output ONLY the five sections in order. Nothing before "## Operator goals", nothing after the last bullet under "## Open threads".
- If a section has nothing to report for this slice, write `- (none in this slice)` as its single bullet.
"""


_APPEND_SYSTEM_PROMPT = """You are extending a running summary of a conversation between the assistant (a personal AI assistant) and the operator (a human builder).

A PRIOR SUMMARY already exists covering earlier turns of the conversation. It is verbatim and complete for those turns. The system will append your output AFTER the prior summary; you MUST NOT rewrite, restate, or compress prior slice content.

PRIOR SUMMARY (for context only — do NOT echo, do NOT re-summarize):
{prior_summary}

YOUR TASK — produce a structured summary covering ONLY the new slice of conversation provided below. Use EXACTLY these section headers, in this order:

## Operator goals
## Decisions made
## Files touched
## Facts learned
## Open threads

Rules:
- Cover ONLY the new slice. Anything already in the prior summary stays there — do not repeat it.
- Use the section headers verbatim. No other H2 headers.
- Bullet points under each section. Terse — at most 8 bullets per section.
- Plain factual statements; no commentary, no preamble, no closing remarks.
- Output ONLY the five sections. Nothing before "## Operator goals", nothing after the last bullet under "## Open threads".
- If a section has nothing new to report, write `- (none new in this slice)`.
"""


async def compact_history(
    adapter: ModelAdapter,
    options: AdapterOptions,
    history_to_summarize: list[dict[str, Any]],
    *,
    prior_summary: str | None = None,
) -> str:
    """Summarize a slice of history into a structured 5-section block.

    When ``prior_summary`` is provided, the summarizer is told the
    prior content is verbatim-preserved by the caller and it must only
    cover the new slice. The returned text is the section block for
    THIS slice only — the caller is responsible for concatenating it
    with the prior summary under a fresh ``# Slice N`` header.

    Runs a standalone adapter call — no shared history, no tools. On
    failure returns ``""`` and the caller keeps the existing history.

    Compaction never falls back to a different model. The summary
    overwrites session memory permanently and seeds every subsequent
    turn until the next compaction; a different voice means a different
    memory. If ``adapter`` is a ``FallbackAdapter``, we unwrap to its
    primary and run the summarizer on that alone.
    """
    if not history_to_summarize:
        return ""

    # Avoid circular import — adapter_chain depends on this module's siblings.
    from tesseract.brain.adapter_chain import FallbackAdapter

    if isinstance(adapter, FallbackAdapter):
        primary_options = adapter.primary_options
        adapter = adapter.primary
        options = primary_options
        logger.info(
            "compaction: using primary only (%s); fallback chain bypassed",
            options.model or "<unset>",
        )

    if prior_summary:
        system_prompt = _APPEND_SYSTEM_PROMPT.format(prior_summary=prior_summary)
        closing_instruction = (
            "Now produce the section block for the NEW SLICE ONLY. "
            "Do not echo the prior summary."
        )
    else:
        system_prompt = _FRESH_SYSTEM_PROMPT
        closing_instruction = (
            "Now produce the five-section summary of the slice above."
        )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        *_strip_tool_messages(history_to_summarize),
        {"role": "user", "content": closing_instruction},
    ]

    parts: list[str] = []
    try:
        async for chunk in adapter.stream(messages=messages, options=options):
            if chunk.type == ChunkType.TEXT:
                parts.append(chunk.text)
            elif chunk.type == ChunkType.ERROR:
                logger.warning("compaction adapter error: %s", chunk.error)
                return ""
    except Exception as e:
        logger.exception("compaction failed: %s", e)
        return ""

    summary = "".join(parts).strip()
    # CR-0 M6 — guard against malformed adapter output. The prompt asks
    # for 5 named ## sections; if any are missing the response is prose
    # or partial. Returning "" triggers the caller's "keep full history"
    # branch — same fallback as the empty-summary path.
    if not _validate_structured_summary(summary):
        logger.warning(
            "compaction: model returned malformed summary (missing required "
            "sections); keeping full history. First 200 chars: %r",
            summary[:200],
        )
        return ""
    return summary


def _validate_structured_summary(summary: str) -> bool:
    """True when ``summary`` contains every required ``## `` section header.

    Empty / whitespace-only / non-string input → False. The check is
    substring-based to tolerate the model adding the optional
    ``(turns N..M)`` decoration after the header. Order is not enforced
    (operators have observed Gemini occasionally swapping the last two
    sections; substantively the same summary).
    """
    if not isinstance(summary, str) or not summary.strip():
        return False
    return all(header in summary for header in SECTION_HEADERS)


def _strip_tool_messages(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop tool-call and tool-result messages from the summarizer input.

    The summarizer is running without the tool registry, so passing
    `role: tool` messages or assistant messages with `tool_calls` can
    confuse some providers. We keep only plain user/assistant text.
    """
    out: list[dict[str, Any]] = []
    for msg in history:
        role = msg.get("role")
        if role == "tool":
            continue
        if role == "assistant" and msg.get("tool_calls"):
            content = msg.get("content") or ""
            if content:
                out.append({"role": "assistant", "content": content})
            continue
        content = msg.get("content", "")
        out.append({"role": role, "content": _text_for_compaction(content)})
    return out


def _text_for_compaction(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind in ("text", "input_text", "output_text"):
            text = item.get("text") or item.get("content") or ""
            if isinstance(text, str) and text:
                parts.append(text)
        elif kind == "image":
            filename = item.get("filename") or "image"
            parts.append(f"[attached image: {filename}]")
        elif kind == "file":
            filename = item.get("filename") or "file"
            parts.append(f"[attached file: {filename}]")
    return " ".join(parts).strip()
