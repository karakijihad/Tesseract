"""Telegram outbound chunker (audit fix M2).

Telegram caps single ``sendMessage`` bodies at 4096 characters. The bridge
historically sliced ``body[:4000]`` and discarded the tail, leading to
silent truncation of "get the latest info" / mission status replies. This
module splits long bodies on paragraph boundaries, never inside a code
fence, and labels each chunk with ``(n/N)`` so the operator sees the
continuation.

Rules:

- Chunks are ordered. Each chunk fits within ``max_len`` *including* the
  ``(n/N)`` suffix for chunks 2…N.
- Paragraph boundaries (``\n\n``) are preferred. When a single paragraph
  exceeds the budget we fall back to line splits, then to a hard slice
  inside a word — emergency only.
- Code fences (```) are kept intact: if a chunk would land mid-fence, we
  close it with a trailing fence and reopen at the next chunk.
- For a single-chunk body, no suffix is appended. The contract is
  ``chunk_for_telegram("short")`` → ``["short"]``.
- An empty / whitespace-only body returns ``[]`` so callers can skip the
  send call without a defensive ``if text:`` guard.
"""

from __future__ import annotations

import re
from typing import List

# Telegram's documented hard cap is 4096 utf-16 code units. We reserve 96
# for the ``(n/N)`` suffix + a safety margin against multi-byte runes that
# expand under utf-16 encoding (rare; emoji-heavy bodies). 4000 was the
# previous flat cap — preserved for behavioural compatibility on chunk 1.
TELEGRAM_TEXT_MAX = 4000

_FENCE_RE = re.compile(r"```")


def chunk_for_telegram(text: str, *, max_len: int = TELEGRAM_TEXT_MAX) -> List[str]:
    """Split ``text`` into a list of Telegram-safe chunks.

    Empty / whitespace-only input returns ``[]``. A body that already
    fits returns a single-element list without a ``(1/1)`` suffix.
    """
    if not text or not text.strip():
        return []
    body = text.rstrip("\n")
    if len(body) <= max_len:
        return [body]

    # Reserve room for the longest suffix we will append. ``(99/99)`` is
    # 7 chars + leading newlines; budget 16 to be safe even for ``(N/N)``
    # with N up to 999 (≈ 4 MB of source text, far beyond realistic).
    budget = max_len - 16
    if budget < 256:
        budget = max_len  # tiny test caps — accept overshoot rather than infinite loop

    raw_chunks = _split_to_budget(body, budget=budget)
    chunks = _balance_code_fences(raw_chunks)
    total = len(chunks)
    if total == 1:
        return chunks
    out: List[str] = []
    for idx, chunk in enumerate(chunks, start=1):
        suffix = f"\n\n({idx}/{total})"
        out.append(chunk + suffix)
    return out


def _split_to_budget(body: str, *, budget: int) -> List[str]:
    """Greedy paragraph-then-line split, no chunk above ``budget``."""
    paragraphs = body.split("\n\n")
    chunks: List[str] = []
    current = ""
    for para in paragraphs:
        candidate = para if not current else current + "\n\n" + para
        if len(candidate) <= budget:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(para) <= budget:
            current = para
            continue
        # Paragraph alone overflows — split on lines, then words.
        for sub in _split_long_paragraph(para, budget=budget):
            if not current:
                current = sub
                continue
            candidate2 = current + "\n" + sub
            if len(candidate2) <= budget:
                current = candidate2
            else:
                chunks.append(current)
                current = sub
    if current:
        chunks.append(current)
    return chunks


def _split_long_paragraph(para: str, *, budget: int) -> List[str]:
    lines = para.split("\n")
    out: List[str] = []
    current = ""
    for line in lines:
        if len(line) > budget:
            # Single line longer than the budget — slice by words; if a
            # word is itself oversized, hard-cut it. Loops are bounded
            # because each iteration removes ``budget`` chars.
            if current:
                out.append(current)
                current = ""
            for piece in _hard_slice(line, budget=budget):
                out.append(piece)
            continue
        candidate = line if not current else current + "\n" + line
        if len(candidate) <= budget:
            current = candidate
        else:
            if current:
                out.append(current)
            current = line
    if current:
        out.append(current)
    return out


def _hard_slice(text: str, *, budget: int) -> List[str]:
    out: List[str] = []
    remaining = text
    while len(remaining) > budget:
        # Try not to bisect a word: rewind to the last space within
        # the trailing 64 chars of the slice point. If none, hard-cut.
        cut = budget
        space = remaining.rfind(" ", max(0, budget - 64), budget)
        if space > 0:
            cut = space
        out.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        out.append(remaining)
    return out


def _balance_code_fences(chunks: List[str]) -> List[str]:
    """If a chunk leaves an unmatched ``` open, close it and reopen on next."""
    if len(chunks) <= 1:
        return chunks
    out: List[str] = []
    carry_open: bool = False
    for chunk in chunks:
        opening = "```\n" if carry_open else ""
        body = opening + chunk
        fence_count = len(_FENCE_RE.findall(body))
        if fence_count % 2 == 1:
            body = body.rstrip() + "\n```"
            carry_open = True
        else:
            carry_open = False
        out.append(body)
    return out


__all__ = ["TELEGRAM_TEXT_MAX", "chunk_for_telegram"]
