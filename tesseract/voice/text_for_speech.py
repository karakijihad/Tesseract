"""Markdown → spoken-prose normaliser for the TTS surface.

The model emits the same string to both the visible chat surface (where
markdown renders) and the speech surface (where TTS reads it character
for character). Without preprocessing, listeners hear "backtick backtick
backtick python", "hash hash Section", "dash item one dash item two".

`to_spoken_text` strips constructs that have no spoken value while
preserving the prose around them. It runs once per sentence inside
`_synthesize_sentence_audio` — sentence-level scope avoids streaming
state issues with multi-line code fences.

Provider-side `_sanitize_for_tts` keeps responsibility for in-line style
cues (`[whispers]`) and emphasis asterisks (`**bold**`); this function
owns markdown structure.
"""

from __future__ import annotations

import re

_FENCED_CODE_RE = re.compile(r"```[^\n]*\n.*?```", re.DOTALL)
# Trailing/unterminated fence: matches an opening ``` (with optional
# language tag) whether or not it has a body. The `(?:\n.*)?` keeps the
# narrow streaming-fragment case — a bare opening line with no following
# newline yet — from leaking three literal backticks into the speech.
_FENCED_CODE_TRAILING_RE = re.compile(r"```[^\n]*(?:\n.*)?$", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`+([^`]+)`+")
_HEADER_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.MULTILINE)
_BULLET_RE = re.compile(r"^[ \t]*[-*+][ \t]+", re.MULTILINE)
_NUMBERED_RE = re.compile(r"^[ \t]*\d+[.)][ \t]+", re.MULTILINE)
_HRULE_RE = re.compile(r"^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$", re.MULTILINE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_BLOCKQUOTE_RE = re.compile(r"^[ \t]*>[ \t]?", re.MULTILINE)
_WHITESPACE_RE = re.compile(r"\s+")


def to_spoken_text(text: str) -> str:
    """Return a spoken-friendly rendering of `text`.

    Drops fenced code blocks and horizontal rules entirely; strips
    headers, list markers, blockquote markers, HTML tags, link/image
    syntax (keeps the link label, drops the URL), and inline-code
    backticks. Returns "" if nothing speakable remains.
    """
    if not text:
        return ""
    cleaned = _IMAGE_RE.sub("", text)
    cleaned = _FENCED_CODE_RE.sub("", cleaned)
    cleaned = _FENCED_CODE_TRAILING_RE.sub("", cleaned)
    cleaned = _LINK_RE.sub(r"\1", cleaned)
    cleaned = _INLINE_CODE_RE.sub(r"\1", cleaned)
    cleaned = _HRULE_RE.sub("", cleaned)
    cleaned = _HEADER_RE.sub("", cleaned)
    cleaned = _BULLET_RE.sub("", cleaned)
    cleaned = _NUMBERED_RE.sub("", cleaned)
    cleaned = _BLOCKQUOTE_RE.sub("", cleaned)
    cleaned = _HTML_TAG_RE.sub("", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned
