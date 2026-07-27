"""Sanitize advisor worker output before it is tail-sliced into a summary.

Root cause of broken ``}`` autonomy proposal titles: raw tool output was
blind-sliced by its last N chars into ``WorkerRecord.summary``, and a
"first sentence" was then extracted from that slice. When the slice
boundary landed mid-JSON or mid-code-fence, the extracted title was
garbage (e.g. a lone ``}``). This module strips fenced code/JSON before
the tail slice, then strips leading fragment debris left AT the new
boundary the slice just created, so the returned tail always starts on
real prose.
"""

from __future__ import annotations

import re

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_UNTERMINATED_FENCE_RE = re.compile(r"```.*\Z", re.DOTALL)
_WHITESPACE_RE = re.compile(r"\s+")
_LEADING_DEBRIS_RE = re.compile(r"^[^A-Za-z0-9\s]+\s*")
_KEEP_PREFIXES = ("we should ", "please ")


def clean_summary_tail(raw: str, *, tail_chars: int) -> str:
    if not raw or not raw.strip():
        return ""

    text = _FENCE_RE.sub(" ", raw)
    text = _UNTERMINATED_FENCE_RE.sub(" ", text)

    text = _WHITESPACE_RE.sub(" ", text).strip()

    if len(text) > tail_chars:
        text = text[-tail_chars:]

    if not text.lower().startswith(_KEEP_PREFIXES):
        text = _LEADING_DEBRIS_RE.sub("", text)

    first_non_space = text.lstrip(" ")
    if not first_non_space or not first_non_space[0].isalnum():
        return ""

    return text
