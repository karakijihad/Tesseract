"""Body-level `## Related` section sync for Obsidian graph compatibility.

`auto_links` is a frontmatter list — Obsidian's graph view doesn't follow
custom YAML keys. To make inter-memory edges visible *without* changing
the canonical `auto_links` field that retrieval depends on, we mirror the
list into a delimited Markdown block at the end of each memory body:

    <!-- auto-related:start -->
    ## Related

    - [[mem_xxx|Title]]
    - [[mem_yyy]]
    <!-- auto-related:end -->

Items can be passed as bare `mem_xxx` IDs (legacy) or as `(mem_xxx, title)`
tuples. Tuples render the Obsidian alias form `[[id|title]]` so a raw
file reader sees something meaningful. Empty/None titles fall back to the
bare-ID form. Frontmatter `auto_links` stays as bare IDs — titles are
body-only sugar.

The HTML comments are invisible in Obsidian preview but uniquely
identify the auto-managed block so `replace_related_block` can swap it
without touching operator-edited prose elsewhere in the body.
"""

from __future__ import annotations

import re
from typing import Sequence, Union

START_MARKER = "<!-- auto-related:start -->"
END_MARKER = "<!-- auto-related:end -->"

RelatedItem = Union[str, tuple[str, str]]

_BLOCK_RE = re.compile(
    r"\n*" + re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER) + r"\n*",
    re.DOTALL,
)


def _normalize(items: Sequence[RelatedItem]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for it in items:
        if isinstance(it, tuple):
            mid, title = it[0], (it[1] or "") if len(it) > 1 else ""
        else:
            mid, title = it, ""
        if mid:
            out.append((mid, title))
    return out


def render_related_block(items: Sequence[RelatedItem]) -> str:
    """Render the `## Related` block for the given items.

    Items can be bare memory IDs (legacy) or `(id, title)` tuples. Returns
    an empty string when no items are given so callers can drop the block
    entirely instead of leaving a hollow header.
    """
    pairs = _normalize(items)
    if not pairs:
        return ""
    lines = [START_MARKER, "## Related", ""]
    for mid, title in pairs:
        if title:
            # Obsidian alias form: link target stays the canonical ID, the
            # operator sees the human title in the rendered + raw view.
            lines.append(f"- [[{mid}|{title}]]")
        else:
            lines.append(f"- [[{mid}]]")
    lines.append(END_MARKER)
    return "\n".join(lines)


def replace_related_block(body: str, items: Sequence[RelatedItem]) -> str:
    """Strip any existing auto-managed Related block from `body`, then
    append a fresh one for `items`. Empty `items` drops the block entirely.

    Body lines outside the markers are preserved verbatim so any operator-
    written `## Related` heading without our markers stays untouched.
    """
    stripped = _BLOCK_RE.sub("\n\n", body).rstrip()
    new_block = render_related_block(items)
    if not new_block:
        return stripped + "\n" if stripped else ""
    return stripped + "\n\n" + new_block + "\n"
