"""Streaming assistant-text parser + sentence/paragraph splitters.

Extracted from ``ws.py`` 2026-05-23 (codex audit m2 follow-up). These are
the pure(-ish) state-machine helpers that consume the LLM's tagged stream
contract (``<intent>``/``<answer>`` from ``prompt.py``) and split prose
into TTS-eligible segments.

The only non-pure entry is :func:`_split_text_for_surfaces`, which reads
+ writes carry-state fields (``stream_status_buffer``, ``stream_tag_state``,
``stream_untagged_warned``). mirror-multi-chat P2 inc.C2 moved those off the
shared ``ServerSession`` onto the per-turn ``TurnState`` the caller passes in:
background chats now stream text in parallel, so the parser runs concurrently
and each turn must own its carry state.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from tesseract.mirror.server.session import ServerSession


class _ParserCarry(Protocol):
    """The carry-state slice the parser reads/writes — satisfied by both
    ``TurnState`` (production) and ``_LegacyTurnStateView`` (direct-test path)."""

    stream_status_buffer: str
    stream_tag_state: str
    stream_untagged_warned: bool

log = logging.getLogger(__name__)


# Sentence boundary: a punctuation char followed by whitespace OR an
# uppercase letter (sentence-merge case). Capture group 1 keeps the
# preceding sentence; what follows the whitespace is the next-sentence carry.
_SENTENCE_BOUNDARY_RE = re.compile(r"([\.!\?])(\s+|(?=[A-Z])|$)")
# Paragraph break = a blank line. Stronger break than a sentence; maps to a
# natural prosodic pause when streaming long replies in `speak` mode.
_PARAGRAPH_BOUNDARY_RE = re.compile(r"(\n)(\n+)")
# Soft cap so a punctuation-free monologue still gets some audio out before
# the turn closes; if the buffer grows past this, we flush at the next space
# (or, failing that, at turn end). 200 chars ≈ a long sentence.
_TTS_FORCE_FLUSH_CHARS = 200

# Structured-tag contract — the model is instructed in `prompt.py` to wrap
# every text emission in `<intent>...</intent>` (status before/between
# tools) or `<answer>...</answer>` (final reply). The classifier below is
# a streaming state machine over that contract — no heuristics, no regex
# on free prose. Untagged text degrades to "answer" with a logged warning.
_TAG_OPEN_INTENT = "<intent>"
_TAG_CLOSE_INTENT = "</intent>"
_TAG_OPEN_ANSWER = "<answer>"
_TAG_CLOSE_ANSWER = "</answer>"
_TAG_TOKENS = (
    _TAG_OPEN_INTENT, _TAG_CLOSE_INTENT, _TAG_OPEN_ANSWER, _TAG_CLOSE_ANSWER,
)

_KNOWN_CHANNEL_TAGS = ("intent", "answer")


def _split_text_for_surfaces(
    session: "ServerSession", carry: _ParserCarry, delta: str
) -> list[tuple[str, str]]:
    """Split an assistant text delta into (kind, text) pieces using the
    `<intent>`/`<answer>` tag contract from `prompt.py`.

    Streams safely: a partial tag straddling a delta boundary is held in
    `carry.stream_status_buffer` (the per-turn ``TurnState``) and prepended on
    the next call. Untagged text (model failed to honor the contract) degrades
    to "answer" kind so the operator still sees output, with one `log.warning`
    per turn.

    Channel sessions (Telegram, etc.) have no tag contract — the channel
    overlay explicitly instructs TARS to skip `<intent>` / `<answer>`. We
    short-circuit them as a single ``answer`` piece so a mis-emitted tag
    on a channel can't strand text in ``stream_status_buffer`` or surface
    a contract-violation warning. Channel adapters use
    :func:`_extract_channel_reply` for their own post-stream tag strip;
    this function exists for the cockpit streaming path only.
    """
    if not delta:
        return []
    chat_session = getattr(session, "chat_session", None)
    if getattr(chat_session, "session_kind", None) == "channel":
        normalized = _normalize_assistant_delta(delta)
        if not normalized:
            return []
        carry.stream_status_buffer = ""
        carry.stream_tag_state = "outside"
        return [("answer", normalized)]
    state = carry.stream_tag_state
    held = carry.stream_status_buffer
    buffer = held + _normalize_assistant_delta(delta)
    pieces, new_state, new_carry = _parse_tagged_stream(buffer, state)
    if any(kind == "_untagged" for kind, _ in pieces) and not carry.stream_untagged_warned:
        log.warning(
            "ws: assistant emitted untagged text (degrading to answer) — "
            "model is violating the <intent>/<answer> output contract"
        )
        carry.stream_untagged_warned = True
    carry.stream_tag_state = new_state
    carry.stream_status_buffer = new_carry

    # Coalesce consecutive same-kind pieces for fewer envelopes downstream.
    # `_untagged` collapses into `answer` since that is its surface.
    merged: list[tuple[str, str]] = []
    for kind, text in pieces:
        surface = "answer" if kind == "_untagged" else kind
        if not text:
            continue
        if merged and merged[-1][0] == surface:
            merged[-1] = (surface, merged[-1][1] + text)
        else:
            merged.append((surface, text))
    return merged


def _extract_channel_reply(raw: str) -> str:
    """Strip the `<intent>`/`<answer>` scaffold from a full model reply.

    Channel adapters (Telegram, etc.) concatenate raw text chunks. The
    Mirror UI parses tags via the streaming `_parse_tagged_stream`; channels
    need a one-shot post-process to surface only the operator-visible
    `<answer>` text.

    The channel overlay tells TARS not to emit tags here, but the base
    prompt still teaches the contract and TARS sometimes emits them
    anyway. Worse, a tool-iteration-cap hit can cut the stream after
    ``<intent>...</intent>`` and before any ``<answer>`` opens — the
    pre-fix code path returned the raw text in that case, leaking
    literal ``<intent>`` tags to the phone. Order of preference now:

    1. Concatenated ``<answer>`` content (the contracted-on payload).
    2. Untagged text (``_untagged``) — operator wrote freely; respect it.
    3. Stripped ``<intent>`` content — model only emitted intent because
       the stream was truncated. Surface the intent text without tags so
       the user sees "Checking the vault for that." instead of
       ``<intent>Checking the vault for that.</intent>``.
    4. As a last resort, the raw input with bare ``<intent>``/``<answer>``
       tags stripped so a malformed-tag edge case (e.g. mismatched close)
       at least renders without raw markup.
    """
    pieces, _state, _carry = _parse_tagged_stream(raw, "outside")
    answers = [text for kind, text in pieces if kind == "answer"]
    if answers:
        return "".join(answers)
    untagged = [text for kind, text in pieces if kind == "_untagged"]
    if untagged:
        return "".join(untagged)
    intents = [text for kind, text in pieces if kind == "intent"]
    if intents:
        return "".join(intents)
    return _strip_known_tags(raw)


def _strip_known_tags(text: str) -> str:
    """Drop literal ``<intent>`` / ``</intent>`` / ``<answer>`` / ``</answer>``
    occurrences from ``text``. Last-resort fallback when the parser found
    nothing classifiable — never leak tag text to the phone."""
    pattern = re.compile(
        r"</?(?:" + "|".join(_KNOWN_CHANNEL_TAGS) + r")\s*>",
        re.IGNORECASE,
    )
    return pattern.sub("", text).strip()


def _parse_tagged_stream(buffer: str, state: str) -> tuple[list[tuple[str, str]], str, str]:
    """Pure-function state machine over the tagged stream.

    Returns `(pieces, new_state, carry)`:
    - `pieces` — list of `(kind, text)`. `kind` ∈ {"intent", "answer", "_untagged"}.
      `_untagged` is content emitted while state was "outside" (contract
      violation); the caller surfaces it under "answer".
    - `new_state` — state at end of input, one of {"outside", "intent", "answer"}.
    - `carry` — tail of `buffer` that may be the start of an incomplete tag;
      held back so it can be re-evaluated when the next delta arrives.
    """
    pieces: list[tuple[str, str]] = []
    pos = 0
    n = len(buffer)
    while pos < n:
        next_idx = -1
        next_token = ""
        for tok in _TAG_TOKENS:
            idx = buffer.find(tok, pos)
            if idx != -1 and (next_idx == -1 or idx < next_idx):
                next_idx = idx
                next_token = tok

        if next_idx == -1:
            tail = buffer[pos:]
            partial = _partial_tag_suffix_len(tail)
            content = tail[: len(tail) - partial] if partial else tail
            new_carry = tail[len(tail) - partial:] if partial else ""
            if content:
                kind = state if state in ("intent", "answer") else "_untagged"
                pieces.append((kind, content))
            return pieces, state, new_carry

        before = buffer[pos:next_idx]
        if before:
            kind = state if state in ("intent", "answer") else "_untagged"
            pieces.append((kind, before))

        if next_token == _TAG_OPEN_INTENT:
            state = "intent"
        elif next_token == _TAG_OPEN_ANSWER:
            state = "answer"
        elif next_token in (_TAG_CLOSE_INTENT, _TAG_CLOSE_ANSWER):
            state = "outside"

        pos = next_idx + len(next_token)

    return pieces, state, ""


def _partial_tag_suffix_len(tail: str) -> int:
    """Return the number of trailing characters of `tail` that could be the
    start of an unfinished tag token. Zero if no prefix match.
    """
    max_check = min(len(tail), max(len(t) for t in _TAG_TOKENS) - 1)
    for k in range(max_check, 0, -1):
        suffix = tail[-k:]
        if suffix.startswith("<") and any(tok.startswith(suffix) for tok in _TAG_TOKENS):
            return k
    return 0


def _normalize_assistant_delta(delta: str) -> str:
    return delta.replace("\\r\\n", "\n").replace("\\n", "\n")


def _split_sentences(buffer: str) -> tuple[list[str], str]:
    """Greedily peel sentences off the front of `buffer` at `[.!?]\\s` boundaries.
    Returns `(complete_sentences, remaining_tail)`. Empty/whitespace-only
    sentences are dropped — they yield no useful audio. When the buffer has
    no boundary at all but exceeds the soft cap, flush it as a single
    sentence to keep audio progressing on a punctuation-free monologue."""
    sentences: list[str] = []
    cursor = 0
    while True:
        match = _SENTENCE_BOUNDARY_RE.search(buffer, cursor)
        if match is None:
            break
        end = match.end(1)
        sentence = buffer[cursor:end].strip()
        if sentence:
            sentences.append(sentence)
        cursor = match.end(2)
    remaining = buffer[cursor:]
    if not sentences and len(remaining) >= _TTS_FORCE_FLUSH_CHARS:
        space_idx = remaining.rfind(" ")
        if space_idx > 0:
            sentence = remaining[:space_idx].strip()
            if sentence:
                sentences.append(sentence)
            remaining = remaining[space_idx + 1:]
    return sentences, remaining


def _split_speak_segments(buffer: str) -> tuple[list[str], str]:
    """Split `buffer` into flushable speak-mode segments.

    Paragraph breaks (`\\n\\n+`) are the strongest boundary — they map to
    natural prosodic pauses, so we prefer them. Sentence boundaries
    (`[.!?]\\s`) are the fallback for long single paragraphs that would
    otherwise stall mid-turn audio. Empty/whitespace-only segments are
    dropped. Returns (segments, remaining_tail).
    """
    segments: list[str] = []
    cursor = 0
    while True:
        para = _PARAGRAPH_BOUNDARY_RE.search(buffer, cursor)
        sent = _SENTENCE_BOUNDARY_RE.search(buffer, cursor)
        if para is None and sent is None:
            break
        if para is not None and (sent is None or para.start() <= sent.start()):
            end = para.start(1)
            advance = para.end(2)
        else:
            end = sent.end(1)
            advance = sent.end(2)
        segment = buffer[cursor:end].strip()
        if segment:
            segments.append(segment)
        cursor = advance
    return segments, buffer[cursor:].lstrip()
