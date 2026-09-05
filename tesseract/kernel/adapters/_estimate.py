"""One divisor, measured, for every token estimate in the runtime.

No adapter carries a tokenizer: `count_tokens` is called synchronously from
compaction's budget maths and from the chain's context-window guard, which has
to ask an entry for an estimate WITHOUT constructing it. So the estimate is
characters divided by a constant, and the constant used to be written `// 4`
inline in four adapters and once in `tokenjuice/audit.py`.

**Four was wrong for this runtime's content, and it was wrong in the direction
that hides a problem.** Measured 2026-08-26 over 237 consecutive-turn pairs in
`sessions/chats/*.json`, where the delta in recorded characters and the delta
in the provider's own `usage.input_tokens` describe the same new content:

    d_tokens = d_chars / 3.316 + 368        r^2 = 0.633

The intercept is the late-half prompt, which rides every turn and is never
written to history, so it adds tokens against no recorded characters; the
regression is what separates it from the divisor rather than letting it bias
it. The slope is the answer: **3.32 characters per token**, over a history full
of code, JSON and tool results, which tokenize far denser than prose. At 4.0
the runtime believed a 765,000-character payload was 191,250 tokens when the
provider charged for 230,706 — a 21 percent undercount, on the number that
decides when a conversation folds.

This is a measured property of tokenization, not an operator preference, which
is why it is a constant here and not a dial in `roles.yaml`. To re-measure it
when the content mix changes materially: regress provider-reported token counts
against payload characters over a real history, take the slope rather than the
mean ratio so a fixed per-request overhead does not bias it, and correct the
constant here.

What each adapter counts stays each adapter's business: an image is worth a
flat token count rather than its characters, an OpenAI reasoning blob is
counted even though nobody can read it, and Anthropic counts tool `input`
blocks. Only the conversion lives here.
"""

from __future__ import annotations

#: Characters per token. See the module docstring for how it was measured.
CHARS_PER_TOKEN = 3.316


def tokens_from_chars(chars: int) -> int:
    """Characters to an estimated token count.

    Rounds up, and any non-empty text is worth at least one token: an estimate
    that returns zero for real content reads to every caller as "nothing here",
    and the callers are a fold decision and a context-window guard.
    """
    if chars <= 0:
        return 0
    return max(1, int(chars / CHARS_PER_TOKEN + 0.5))
