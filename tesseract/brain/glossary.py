"""Every tool the assistant has, one line each, grouped by the question asked.

The schema payload carries a subset of the registry and carries it in the
adapter's own shape — a name, a description, a JSON Schema — with no ordering
and no relationship between one tool and the next. It is a list of doors with
no map of the building.

This is the map. Every tool TESSERACT ships appears, under the heading for
the question it answers, whether or not its schema is in the payload this turn:
knowing a capability exists is what makes `tool_search` reachable at all, and a
tool the model cannot name is a tool it will not look for.

Tools the operator wrote (`origin == "custom"`) are deliberately NOT here. The
map is rendered on every turn and everything on it is paid for on every turn,
so a directory someone fills would grow the prompt with their filesystem.
`tool_search` reaches them, and returns the same description this would have
shown.

Headings and their order come from `kernel/tools/taxonomy.py`, which is
authoritative and checked at boot. Nothing here is written by hand — the roster
is the registry, the summaries are `Tool.summary`, and the count is counted.
"""

from __future__ import annotations

from tesseract.kernel.tools import taxonomy

_HEADER = "# Every tool you have"

_PREAMBLE = (
    "Grouped by the question you are answering. A tool's full description — "
    "when to reach for it, and which tool outranks it — comes with its "
    "schema, so read that before choosing between two that sound alike."
)

#: The per-tool marker, and the sentence that defines it. Naming which side of
#: the line each tool falls on is the point: the map used to say "some arrive
#: as callable schemas, the rest are one `tool_search` away", which is true and
#: unusable, because nothing said WHICH. A model scanning for a capability
#: could not tell what a given line would cost it, and one that already
#: believed it knew the answer never found out. That is not hypothetical:
#: `surface_control` sat here correctly summarised while the assistant told the
#: operator that a card it had just drawn could not be operated.
_DEFERRED_MARK = " (search)"


def render(registry) -> str:
    """The glossary, ready to be a prompt section. `""` for an empty registry.

    Tools with no `group` are skipped rather than gathered under an "other"
    heading: the boot guard makes an ungrouped registered tool impossible, so
    the only way to reach that branch is a hand-built registry in a test, and
    inventing a heading for it would hide the drift the guard exists to catch.

    An unknown slug is the opposite case and raises, via `heading_for` — that
    one CAN happen if `taxonomy.GROUPS` loses an entry a tool still claims, and
    a tool silently missing from the map is exactly the failure this replaces.
    """
    by_group: dict[str, list] = {}
    # Snapshot: `home_tools.sync_home_tools` swaps this dict from a worker
    # thread, and a dict that changes size mid-iteration raises.
    for tool in list(registry.tools.values()):
        # Custom tools are not on the map. The glossary is the shipped roster,
        # rendered every turn, and its cost is paid on every turn by every
        # entry in it: a directory the operator fills would make the prompt
        # grow with their filesystem. They are reached through `tool_search`
        # instead, which returns the same `description` the map would have
        # shown, at the moment it is wanted. Promote one and it travels as a
        # schema, which is the surface that carries the contract anyway.
        if getattr(tool, "origin", "shipped") == "custom":
            continue
        group = getattr(tool, "group", "")
        if not group:
            continue
        by_group.setdefault(group, []).append(tool)

    if not by_group:
        return ""

    for slug in by_group:
        taxonomy.heading_for(slug)

    total = sum(len(tools) for tools in by_group.values())
    carried = sum(
        1
        for tools in by_group.values()
        for t in tools
        if getattr(t, "tier", "extended") == "core"
    )
    lines = [
        _HEADER,
        "",
        _PREAMBLE,
        "",
        f"You have {total} of them. {carried} are carried this turn with their "
        f"full schemas. The other {total - carried} are marked (search): call "
        f"`tool_search` for one and its schema arrives, then you can use it. "
        f"Marked does not mean unavailable, it means one step away.",
        "",
    ]

    for slug, heading in taxonomy.GROUPS.items():
        tools = by_group.get(slug)
        if not tools:
            continue
        lines.append(f"## {heading}")
        for tool in sorted(tools, key=lambda t: t.name):
            mark = "" if getattr(tool, "tier", "extended") == "core" else _DEFERRED_MARK
            lines.append(f"- `{tool.name}`{mark} — {tool.summary}")
        lines.append("")

    return "\n".join(lines).rstrip()


__all__ = ["render"]
