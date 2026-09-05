"""Write `config/working_set.yaml` from the live tool registry.

The file says which tools carry a schema on every turn. Two halves, and only
one of them is a person's:

- **The choice** — which names are under `core:`. That is the operator's, made
  by hand or from Conscience, and this script preserves it exactly.
- **Everything else** — the grouping, the one-line description beside each
  name, and the list of what is NOT carried. Those are FACTS ABOUT THE TOOLS
  and are read off the registry every time this runs. A tool's group and
  summary live on its class, so a description here can never be a second
  opinion about what a tool does.

That split is why the annotations are generated rather than written. A
hand-authored comment describes the set someone had in mind on the day
they wrote it; the operator then adds a tool from the panel and the prose
describes a file that is no longer there. Regenerating is the only version
that survives being used.

Usage:
    python -m tesseract.scripts.generate_working_set --check   # exit 1 on drift
    python -m tesseract.scripts.generate_working_set --write
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from tesseract.config.working_set import CORE_KEY, FLOOR, config_path
from tesseract.kernel.tools.taxonomy import GROUPS, heading_for

_BANNER = """\
# Which tools the assistant sees without having to look them up.
#
# Every tool it has is always callable by name. This file decides only which
# ones are described to it on every turn, so it reaches for them directly.
# Everything else it finds with `tool_search`, which costs one extra step and
# nothing else. Taking a tool off this list makes it slower to reach, never
# forbidden — what the assistant is ALLOWED to do is `permissions.yaml`, and
# that is a separate question with a separate answer.
#
# So this is a spending dial. Tool descriptions are roughly half of what a turn
# costs before you have typed anything. Conscience -> Usage shows you the
# figure and which tools you actually call: trim the ones you never use and
# turns get cheaper, add one you reach for often and it stops costing a search
# first.
#
# THE NAMES UNDER `core:` ARE YOURS. Edit them here, or from Conscience. The
# groupings and the descriptions beside them are not: they are read off the
# tools themselves every time this file is written, so they cannot drift from
# what the tools actually do. Regenerate after a hand-edit with
#   python -m tesseract.scripts.generate_working_set --write
#
# `tool_search` is added back if you remove it. It is the door to everything
# not on this list, and without it the assistant can only reach what is
# written here.
#
# A name nothing answers to stops the app at startup and says which one it was,
# because a typo that quietly did nothing is how a tool goes missing for weeks.
#
# An update replaces this file with the new release's version and keeps your
# old one in `config-backup/`. If you have changed the list, copy it back.
"""


#: Tools that register only when their dependency resolves. Their facts are
#: read off the classes rather than off a registry, because whether they are
#: present depends on whose machine this runs on — an operator with API keys
#: and a CI box with none would otherwise generate two different files and
#: `--check` would fail for a reason that has nothing to do with the file.
#: The registered name is carried here rather than read off the class: `name`
#: is an instance property, and instantiating these three needs the very
#: dependency that makes them conditional. A test pins this against
#: `_CONDITIONAL_CORE_TOOL_NAMES` so the two cannot drift.
_CONDITIONAL_CLASSES: tuple[tuple[str, str, str], ...] = (
    ("session_open", "tesseract.kernel.tools.session_tools", "SessionOpenTool"),
    ("invoke_agent", "tesseract.kernel.tools.invoke_agent", "InvokeAgentTool"),
    ("transcribe_audio", "tesseract.kernel.tools.transcribe_audio", "TranscribeAudioTool"),
)


def _registry_facts() -> dict[str, tuple[str, str]]:
    """`{name: (group, summary)}` for every tool, registered or adapter-gated."""
    import importlib

    from tesseract.brain.boot import build_tool_registry

    facts: dict[str, tuple[str, str]] = {}
    for name, module_path, class_name in _CONDITIONAL_CLASSES:
        try:
            cls = getattr(importlib.import_module(module_path), class_name)
        except (ImportError, AttributeError):
            continue  # the tool was renamed or removed; the boot guard says so
        facts[name] = (cls.group, cls.summary)

    registry, *_ = build_tool_registry()
    facts.update({
        name: (type(tool).group, type(tool).summary)
        for name, tool in registry.tools.items()
    })
    return facts


def _current_core(path: Path) -> list[str]:
    """The operator's choice, or the shipped default when there is no file yet.

    Read raw rather than through `load_core_tool_names` so a file the loader
    would refuse can still be regenerated into a valid one — this script is
    part of how someone fixes that file, not another thing that breaks on it.
    """
    if not path.is_file():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return []
    entries = raw.get(CORE_KEY) if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return []
    return [e.strip() for e in entries if isinstance(e, str) and e.strip()]


def render(core: list[str], facts: dict[str, tuple[str, str]]) -> str:
    """The whole file, deterministic for a given (choice, registry).

    A chosen name absent from `facts` is kept and marked rather than dropped:
    silently deleting a name from the operator's set because this run could not
    see the tool is a visibility change nobody asked for. After
    `_registry_facts` that can only mean a name nothing answers to, which is
    what the boot guard refuses to start on.
    """
    chosen = set(core) | FLOOR
    lines: list[str] = [_BANNER, "", f"{CORE_KEY}:"]

    for slug in GROUPS:
        in_group = sorted(n for n in chosen if facts.get(n, ("", ""))[0] == slug)
        if not in_group:
            continue
        lines.append(f"  # {heading_for(slug)}")
        for name in in_group:
            lines.append(f"  - {name}{_annotation(name, facts)}")

    unknown = sorted(n for n in chosen if n not in facts)
    if unknown:
        lines.append("  # NO TOOL ANSWERS TO THESE NAMES — the app will not start")
        for name in unknown:
            lines.append(f"  - {name}")

    rest = sorted(set(facts) - chosen)
    if rest:
        lines += ["", "# Not carried, and one `tool_search` away. Move a line up"]
        lines += ["# into the list above to carry it every turn."]
        for slug in GROUPS:
            in_group = [n for n in rest if facts[n][0] == slug]
            if not in_group:
                continue
            lines.append(f"#   {heading_for(slug)}")
            for name in in_group:
                lines.append(f"#   - {name}{_annotation(name, facts)}")

    return "\n".join(lines) + "\n"


def _annotation(name: str, facts: dict[str, tuple[str, str]]) -> str:
    summary = facts.get(name, ("", ""))[1]
    return f"  # {summary}" if summary else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the file")
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if the file is not what this would write"
    )
    args = parser.parse_args(argv)

    path = config_path()
    wanted = render(_current_core(path), _registry_facts())

    if args.check:
        current = path.read_text(encoding="utf-8") if path.is_file() else ""
        if current == wanted:
            print(f"[ok] {path}")
            return 0
        print(f"[drift] {path} is not what the registry would write")
        print("       run: python -m tesseract.scripts.generate_working_set --write")
        return 1

    if not args.write:
        sys.stdout.write(wanted)
        return 0

    path.write_text(wanted, encoding="utf-8")
    print(f"  wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
