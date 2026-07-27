"""Markdown → Telegram-HTML conversion for outbound channel replies.

Telegram supports a narrow HTML subset: ``<b>``, ``<i>``, ``<u>``, ``<s>``,
``<code>``, ``<pre>``, ``<a href="…">``, ``<tg-spoiler>``. No ``<ul>`` /
``<ol>`` — bullet lists must be rendered with literal "• " prefixes.
HTML mode is chosen over MarkdownV2 because MarkdownV2 escapes 18+ chars
and a single missed escape returns 400 from Bot API.

Conversion order (matters):
  1. HTML-escape the raw text so user-supplied ``<``, ``>``, ``&`` are
     safe inside the eventual <code>/<a>/<b> tags we will insert.
  2. Line-level passes: bullets, headers.
  3. Inline regex passes: links, bold, italic, code.

Inline regexes operate on already-escaped text, so the only characters
they need to think about are the markdown markers themselves.
"""

from __future__ import annotations

import re

_URL_RE = re.compile(r"^https?://[^\s)>]+$", re.IGNORECASE)
_LINK_RE = re.compile(r"\[([^\]\n]+?)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_BOLD_RE = re.compile(r"\*\*([^*\n]+?)\*\*")
_ITALIC_STAR_RE = re.compile(r"(?<![*\w])\*([^*\n]+?)\*(?!\w)")
_ITALIC_UNDER_RE = re.compile(r"(?<![_\w])_([^_\n]+?)_(?!\w)")
_CODE_RE = re.compile(r"`([^`\n]+?)`")
_BULLET_RE = re.compile(r"^\s*[-*•]\s+")
_HEADER_RE = re.compile(r"^\s*#{1,6}\s+(.*)$")


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _convert_links(line: str) -> str:
    def replace(match: re.Match[str]) -> str:
        text, url = match.group(1), match.group(2)
        if not _URL_RE.match(url):
            return match.group(0)
        safe_url = url.replace('"', "%22")
        return f'<a href="{safe_url}">{text}</a>'

    return _LINK_RE.sub(replace, line)


def _convert_inline(line: str) -> str:
    line = _convert_links(line)
    line = _CODE_RE.sub(r"<code>\1</code>", line)
    line = _BOLD_RE.sub(r"<b>\1</b>", line)
    line = _ITALIC_STAR_RE.sub(r"<i>\1</i>", line)
    line = _ITALIC_UNDER_RE.sub(r"<i>\1</i>", line)
    return line


def markdown_to_telegram_html(text: str) -> str:
    """Convert a GitHub-flavored-markdown subset to Telegram HTML.

    Idempotent on plain text. Preserves blank lines. Anything that isn't
    matched stays as-is (post-escape) — better to render a stray ``*`` as
    a literal than to mangle the message.
    """
    if not text:
        return ""
    escaped = _html_escape(text)
    out_lines: list[str] = []
    for line in escaped.split("\n"):
        header = _HEADER_RE.match(line)
        if header:
            inner = _convert_inline(header.group(1).strip())
            out_lines.append(f"<b>{inner}</b>")
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            rest = line[bullet.end():]
            out_lines.append(f"• {_convert_inline(rest)}")
            continue
        out_lines.append(_convert_inline(line))
    return "\n".join(out_lines)
