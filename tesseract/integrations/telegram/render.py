"""How a `Message` reads on Telegram.

Telegram-HTML: the title bold, machine facts monospace, the list as bullets.
This is the only place in the runtime that knows any of that, which is the
whole point of the split: the message is composed once and read here.

Escaping is this file's job and nobody else's. These are the runtime's own
words about text it was handed, a log line, an exception, a job name someone
typed, and one `<` in any of them fails Telegram's HTML parse. The bridge then
ships the plain fallback with every tag stripped, so a report's evidence has to
survive the transport intact.

Prose and machine text are escaped differently, and the message model already
says which is which. `body` and `sections` are written for a person and may
carry light markdown, so they go through the markdown converter, which escapes
first. `facts`, `bullets` and the footer are an id, a provider ref, a log line
or a path: they are escaped and nothing else, because a `*` in a log line is a
`*`.

How the parts are ORDERED, and what is dropped when they do not fit, is not
here: that is the message's own layout and every channel gets the same
answer from `assemble`. Only the markup and the length differ.
"""

from __future__ import annotations

import math
import re
from html import escape as html_escape

from tesseract.integrations.telegram.format import markdown_to_telegram_html
from tesseract.orchestrator.autonomy.message import Message, assemble

#: A one-event ping is one event and needs no more. Telegram accepts 4096 per
#: message; the rest is headroom for the tags this file adds.
MAX_CHARS = 512
#: A message that is more than one event cannot be held to that. Three
#: findings of ten once reached the operator while seven were unreachable
#: from the message, and the daily brief is nothing but parts.
LIST_MAX_CHARS = 3500

#: The tags this file emits, and the only ones Telegram takes. None of them
#: is self-closing, so a cut can be made whole again by closing what is still
#: open at the point it stopped.
_TAG_RE = re.compile(r"<(/?)([a-z-]+)(?:\s[^>]*)?>")


def cut_telegram_html(text: str, room: int) -> str:
    """As much of `text` as fits, with its markup still whole.

    A character offset lands wherever it lands: inside `<cod`, between `<b>`
    and its closer, halfway through `&amp;`. Telegram refuses the message
    then, and the bridge's answer to markup it cannot parse is to resend it
    with EVERY tag stripped, so one cut in the prose costs the whole
    message its formatting.

    So a partial tag and a partial entity are dropped, and whatever is still
    open at the cut is closed. **The whole result is measured against `room`,
    not just the slice.** `assemble` computed the room and does not measure
    what comes back, so a cutter that overran it would be a fit function that
    does not fit.

    It is a fixed point rather than a fixed number of rounds. Reserving space
    for the closers shortens the slice, and a shorter slice can drop a CLOSING
    tag that was inside the longer one, which puts its opener back on the stack
    and grows the reserve again. Two rounds looked like enough and was not:
    fuzzing found it overrunning from three levels of nesting up, which
    `markdown_to_telegram_html` reaches on its own from `# [**text**](url)`.
    Each round shrinks the slice by exactly the overshoot, so it terminates.
    """
    if room <= 1:
        return ""
    keep = room - 1
    while keep > 0:
        head = _drop_partial(text[:keep])
        closers = "".join(f"</{name}>" for name, _ in reversed(open_tags(head)))
        overshoot = len(head) + 1 + len(closers) - room
        if overshoot <= 0:
            return head + "…" + closers
        keep -= overshoot
    return ""


def _drop_partial(head: str) -> str:
    """A slice with any half-written tag or entity at its end removed."""
    if head.rfind("<") > head.rfind(">"):
        head = head[: head.rfind("<")]
    entity = re.search(r"&[#0-9a-zA-Z]*$", head)
    if entity:
        head = head[: entity.start()]
    return head.rstrip()


def open_tags(text: str) -> list[tuple[str, str]]:
    """The tags still open at the end of `text`, outermost first.

    Each entry is `(name, opening tag as written)`. The second half is what
    lets a caller REOPEN one: `<a href="...">` closed and reopened as a bare
    `<a>` is markup Telegram refuses, and the href is the part that mattered.

    Every tag it will see was written by this file, from escaped text, so it
    is well nested and a stack is the whole of it.
    """
    stack: list[tuple[str, str]] = []
    for match in _TAG_RE.finditer(text):
        closing, name = match.group(1), match.group(2)
        if not closing:
            stack.append((name, match.group(0)))
        elif stack and stack[-1][0] == name:
            stack.pop()
    return stack


def render_telegram(message: Message) -> str:
    if message.payload:
        # A row's own output is the thing the operator asked for, not a ping
        # about it. Nothing here cuts it: `chunk_for_telegram` splits it into
        # as many messages as it takes, which is what the handler this
        # replaced already did. Held to the ping budget it arrived cut at 512
        # characters with the rest unrecoverable.
        limit: float = math.inf
    else:
        limit = LIST_MAX_CHARS if message.is_long else MAX_CHARS

    head_parts = []
    if message.mark:
        head_parts.append(html_escape(message.mark))
    if message.title:
        head_parts.append(f"<b>{html_escape(message.title)}</b>")

    footer = ""
    if message.footer:
        label, value = message.footer
        value_html = f"<code>{html_escape(value)}</code>"
        footer = f"{html_escape(label)}: {value_html}" if label else value_html

    return assemble(
        head=" ".join(head_parts),
        body=markdown_to_telegram_html(message.body) if message.body else "",
        facts="\n".join(
            f"{html_escape(label)}: <code>{html_escape(value)}</code>"
            for label, value in message.facts
        ),
        sections=tuple(
            f"<i>{html_escape(label)}</i>\n{markdown_to_telegram_html(prose)}"
            if label else markdown_to_telegram_html(prose)
            for label, prose in message.sections
        ),
        bullets=tuple(html_escape(str(line)) for line in message.bullets),
        bullets_label=(
            f"<i>{html_escape(message.bullets_label)}</i>"
            if message.bullets_label else ""
        ),
        footer=footer,
        limit=limit,
        bullet="\u2022",
        had_parts=bool((message.sections or message.bullets) and not message.body),
        cut=cut_telegram_html,
    )


__all__ = [
    "LIST_MAX_CHARS",
    "MAX_CHARS",
    "cut_telegram_html",
    "open_tags",
    "render_telegram",
]
