"""Telegram outbound chunker.

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

# Telegram's documented hard cap is 4096 utf-16 code units. 4000 leaves room
# for the ``(n/N)`` suffix and a margin, and was the previous flat cap.
TELEGRAM_TEXT_MAX = 4000

_FENCE_RE = re.compile(r"```")

#: A rendered tag anywhere in the body means the caller handed this module
#: Telegram-HTML, where ``` is literal text and not a fence.
_MARKUP_RE = re.compile(r"</?[a-z-]+(?:\s[^>]*)?>")


def text_width(text: str) -> int:
    """What Telegram counts: utf-16 code units, not characters.

    Every budget in this module is measured with this rather than `len`.
    Python counts a code point, and an astral character, which is every
    emoji and a good deal of CJK, is TWO utf-16 code units. Measured before
    this existed: a body of 3000 emoji passed the 4000 test as 3000 and
    reached Telegram as 6000, half again past the hard cap, and the send
    failed into the plain fallback this module exists to avoid.
    """
    return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)


def _index_at_width(text: str, width: int) -> int:
    """The largest slice index whose width is at most `width`.

    A budget is a width and a slice is an index, and they stopped being the
    same number the moment the width stopped being `len`.

    At least 1, so a caller slicing in a loop always makes progress. The one
    consequence: a single character wider than `width` still returns 1, so a
    caller can overshoot by that character. `_fit_chunks` measures what came
    back and re-splits, which is what absorbs it at a real budget.
    """
    used = 0
    for index, char in enumerate(text):
        step = 2 if ord(char) > 0xFFFF else 1
        if used + step > width:
            return max(1, index)
        used += step
    return len(text)


def chunk_for_telegram(text: str, *, max_len: int = TELEGRAM_TEXT_MAX) -> List[str]:
    """Split ``text`` into a list of Telegram-safe chunks.

    Empty / whitespace-only input returns ``[]``. A body that already
    fits returns a single-element list without a ``(1/1)`` suffix.
    """
    if not text or not text.strip():
        return []
    body = text.rstrip("\n")
    if text_width(body) <= max_len:
        return [body]

    # Reserve room for the longest suffix we will append. ``(99/99)`` is
    # 7 chars + leading newlines; budget 16 to be safe even for ``(N/N)``
    # with N up to 999 (≈ 4 MB of source text, far beyond realistic).
    budget = max_len - 16
    if budget < 256:
        budget = max_len  # tiny test caps — accept overshoot rather than infinite loop

    chunks = _fit_chunks(body, budget=budget)
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
        if text_width(candidate) <= budget:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if text_width(para) <= budget:
            current = para
            continue
        # Paragraph alone overflows — split on lines, then words.
        for sub in _split_long_paragraph(para, budget=budget):
            if not current:
                current = sub
                continue
            candidate2 = current + "\n" + sub
            if text_width(candidate2) <= budget:
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
        if text_width(line) > budget:
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
        if text_width(candidate) <= budget:
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
    while text_width(remaining) > budget:
        # Try not to bisect a word: rewind to the last space within
        # the trailing 64 chars of the slice point. If none, hard-cut.
        cut = _index_at_width(remaining, budget)
        space = remaining.rfind(" ", max(0, cut - 64), cut)
        if space > 0:
            cut = space
        out.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        out.append(remaining)
    return out


def _fit_chunks(body: str, *, budget: int) -> List[str]:
    """Split and balance, then make sure balancing did not break the budget.

    The carried opening tags and the closers are added AFTER the split has
    already sized each chunk to fit, and neither is charged against it. One
    `<a href="...">` with a real URL in it is a few hundred characters, so a
    chunk sized to exactly fit came back past Telegram's 4096 hard cap:
    measured at 4322 for a long link spanning several paragraphs. The message
    then fails its HTML send and the bridge resends it with every tag
    stripped, which is the loss this module exists to prevent.

    So the overshoot is measured and the budget is reduced by it, up to a few
    times. A single tag longer than the whole budget cannot be made to fit by
    splitting, and that returns the best effort rather than looping: the plain
    fallback is what catches it, and it is the same answer as before.

    **One balancer runs, not both.** They are two answers to the same question
    and they disagree about what markup the text is in. Running both put
    markdown fences into rendered HTML: a body quoting ``` inside a `<code>`
    element had an odd fence count, so a chunk was closed with a fence and the
    next opened with one, and under `parse_mode="HTML"` those backticks are
    literal characters the operator reads as corruption.
    """
    markup = bool(_MARKUP_RE.search(body))
    balance = _balance_html_tags if markup else _balance_code_fences
    room = budget
    chunks = balance(_split_to_budget(body, budget=room))
    for _ in range(4):
        over = max((text_width(chunk) - budget for chunk in chunks), default=0)
        if over <= 0:
            break
        room -= over
        if room < 256:
            break
        chunks = balance(
            _split_to_budget(body, budget=room)
        )
    return chunks


def _balance_html_tags(chunks: List[str]) -> List[str]:
    """Close the tags a chunk left open and reopen them on the next.

    The same rule as the code fences below, one markup layer along. Callers
    hand this module ALREADY-RENDERED Telegram-HTML and it splits on blank
    lines, so a `<b>` run or a `<code>` block longer than one chunk was cut in
    half: the first chunk failed Telegram's parse and was resent with every
    tag stripped, and the second opened with a stray closer.

    Reopened from the tag as it was written, not from its name, because
    `<a href="...">` reopened as `<a>` is markup Telegram refuses too.

    Plain text has no tags, so this is a no-op on everything that is not
    marked up, which is most of what goes through here.
    """
    if len(chunks) <= 1:
        return chunks
    from tesseract.integrations.telegram.render import open_tags

    out: List[str] = []
    carry: List[tuple[str, str]] = []
    for chunk in chunks:
        body = "".join(raw for _, raw in carry) + chunk
        carry = open_tags(body)
        if carry:
            body = body + "".join(f"</{name}>" for name, _ in reversed(carry))
        out.append(body)
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


__all__ = ["TELEGRAM_TEXT_MAX", "chunk_for_telegram", "text_width"]
