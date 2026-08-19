"""Every tool the assistant has, one line each, grouped by the question asked.

The schema payload carries a subset of the registry and carries it in the
adapter's own shape — a name, a description, a JSON Schema — with no ordering
and no relationship between one tool and the next. It is a list of doors with
no map of the building.

This is the map. Every registered tool appears, under the heading for the
question it answers, whether or not its schema is in the payload this turn:
knowing a capability exists is what makes `tool_search` reachable at all, and a
tool the model cannot name is a tool it will not look for.

Headings and their order come from `kernel/tools/taxonomy.py`, which is
authoritative and checked at boot. Nothing here is written by hand — the roster
is the registry, the summaries are `Tool.summary`, and the count is counted.
"""

from __future__ import annotations

from tesseract.kernel.tools import taxonomy

_HEADER = "# Every tool you have"

_PREAMBLE = (
    "Grouped by the question you are answering. Only some arrive as callable "
    "schemas on a given turn; the rest are one `tool_search` away. A tool's "
    "full description — when to reach for it, and which tool outranks it — "
    "comes with its schema, so read that before choosing between two that "
    "sound alike."
)


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
    for tool in registry.tools.values():
        group = getattr(tool, "group", "")
        if not group:
            continue
        by_group.setdefault(group, []).append(tool)

    if not by_group:
        return ""

    for slug in by_group:
        taxonomy.heading_for(slug)

    total = sum(len(tools) for tools in by_group.values())
    lines = [_HEADER, "", _PREAMBLE, "", f"You have {total} of them.", ""]

    for slug, heading in taxonomy.GROUPS.items():
        tools = by_group.get(slug)
        if not tools:
            continue
        lines.append(f"## {heading}")
        for tool in sorted(tools, key=lambda t: t.name):
            lines.append(f"- `{tool.name}` — {tool.summary}")
        lines.append("")

    return "\n".join(lines).rstrip()


__all__ = ["render"]
