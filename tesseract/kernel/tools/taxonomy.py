"""The nineteen groups a tool can belong to.

Groups are named for **the question being asked**, not for the subsystem the
tool lives in: the group is chosen before the tool is, so the heading does the
first half of the disambiguation. "Driving a web page" and "Showing the
operator something" are different sentences, and the tool that made this
necessary — `browser_navigate` — reads as both until a heading separates them.

This list is authoritative. `Tool.group` is checked against it at boot, so a
tool placed in a slug that does not exist here is a startup failure rather than
a tool silently missing from the glossary. It is also the only artifact in the
instruction surface that no code can derive, which is why it is a written
constant rather than something computed from the registry.

Order is glossary order.
"""

from __future__ import annotations

# slug → heading, in the order the glossary renders them.
GROUPS: dict[str, str] = {
    "remembering": "Remembering",
    "research-library": "Research library",
    "searching-the-web": "Searching the web",
    "files-on-disk": "Files on disk",
    "showing-the-operator": "Showing the operator something",
    "driving-a-web-page": "Driving a web page",
    "looking-for-yourself": "Looking for yourself",
    "handing-work-off": "Handing work to someone else",
    "long-running-collaborators": "Long-running collaborators",
    "tracking-spawned-work": "Tracking what you spawned",
    "running-commands": "Running commands",
    "reaching-the-operator": "Reaching the operator elsewhere",
    "asking-without-blocking": "Asking without blocking",
    "being-present": "Being present in the Mirror",
    "time": "Time",
    "projects": "Projects",
    "extending-yourself": "Extending yourself",
    "checking-your-state": "Checking your own state",
    "finding-a-tool": "Finding a tool",
}


def heading_for(slug: str) -> str:
    """The glossary heading for `slug`. Raises `KeyError` on an unknown slug —
    the caller is rendering a section that has no name, and guessing one would
    put a tool under a heading nobody chose."""
    return GROUPS[slug]
