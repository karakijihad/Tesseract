"""Report workspace documents that still carry superseded instructions.

Seeding is additive by design: a document the operator already has is never
overwritten, because these files are prose they edit. That is the right
default and it is also why a correction made to a shipped template never
reaches an install that predates it.

The correction that matters is the write-path one. `workshop/notes.md` is
right; `tesseract/workshop/notes.md` names a nonexistent subfolder of the
state root, matches no policy rule, and therefore falls through to the
security mode's default — ASK in `max`, but AUTO in `headless`. So a stale
document does not merely read oddly, it steers writes at a path whose
permission outcome depends on the mode.

This reports; it does not rewrite. Which paragraphs of their own prose to
keep is the operator's call, and a script cannot make it for them.

    python -m tesseract.scripts.check_workspace_docs
    python -m tesseract.scripts.check_workspace_docs --diff
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

from tesseract.paths import TESSERACT_DIR, workspace_dir

# A `tesseract/`-prefixed write target inside a fenced tool call or inline
# code. Deliberately narrow: the documents legitimately DISCUSS the prefixed
# form in order to warn against it, and matching that prose would flag every
# corrected file as stale.
_STALE_WRITE_PATH = re.compile(
    r"""(?<!never\ )(?<!not\ )["'`]tesseract/(?:workshop|memory-store|workspace|vault)/"""
)


def _shipping_dir() -> Path:
    return TESSERACT_DIR / "workspace" / "_shipping"


#: Frontmatter keys retired 2026-08-14. They were hand-maintained and read
#: by nothing: every workspace document is `_strip_frontmatter`'d before it
#: reaches the prompt, so the numbers only ever disagreed with each other —
#: the shipped operating doc said version 3 while the seeded one said 2, and no
#: behaviour anywhere depended on either. Listed so they cannot drift back
#: in one file at a time.
RETIRED_FRONTMATTER_KEYS = ("version", "last_updated")

_RETIRED_KEY = re.compile(
    rf"^\s*(?:{'|'.join(RETIRED_FRONTMATTER_KEYS)})\s*:", re.MULTILINE
)


def _stale_lines(text: str) -> list[tuple[int, str]]:
    return [
        (number, line.strip())
        for number, line in enumerate(text.splitlines(), start=1)
        if _STALE_WRITE_PATH.search(line)
    ]


def _frontmatter(text: str) -> str:
    """The frontmatter block, or empty when the document has none."""
    if not text.startswith("---\n"):
        return ""
    parts = text.split("---\n", 2)
    return parts[1] if len(parts) == 3 else ""


def retired_keys(text: str) -> list[str]:
    """Retired frontmatter keys still present in `text`.

    Scoped to the frontmatter block on purpose — the documents discuss
    versions in prose, and matching that would flag every file.
    """
    block = _frontmatter(text)
    if not block:
        return []
    return [
        line.split(":", 1)[0].strip()
        for line in block.splitlines()
        if _RETIRED_KEY.match(line)
    ]


def missing_templates(referenced: set[str], templates: Path) -> list[str]:
    """Documents the prompt reads that no shipped template provides.

    A fresh install seeds from `_shipping/` alone. A document named by the
    prompt assembler but absent there is not a build failure and not a
    crash — `_read_file` logs "workspace file missing" and returns empty,
    so the section simply is not there, on every new install, silently.
    """
    present = {p.name for p in templates.glob("*.md")}
    return sorted(referenced - present)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--diff",
        action="store_true",
        help="show a unified diff against the shipped template for each stale file",
    )
    args = parser.parse_args(argv)

    templates = _shipping_dir()
    if not templates.is_dir():
        print(f"no shipped templates at {templates}", file=sys.stderr)
        return 2

    live_root = workspace_dir()
    stale: list[Path] = []
    missing: list[str] = []

    for template in sorted(templates.glob("*.md")):
        live = live_root / template.name
        if not live.exists():
            missing.append(template.name)
            continue
        text = live.read_text(encoding="utf-8")
        hits = _stale_lines(text)
        if not hits:
            continue
        stale.append(live)
        print(f"\n{live.name} — {len(hits)} superseded write-path reference(s):")
        for number, line in hits:
            print(f"  {number}: {line}")
        if args.diff:
            print()
            for line in difflib.unified_diff(
                template.read_text(encoding="utf-8").splitlines(),
                text.splitlines(),
                fromfile=f"shipped/{template.name}",
                tofile=f"yours/{live.name}",
                lineterm="",
            ):
                print(f"  {line}")

    if missing:
        print(f"\nnot seeded yet ({len(missing)}): {', '.join(missing)}")

    if not stale:
        print("every seeded workspace document is current.")
        return 0

    print(
        f"\n{len(stale)} document(s) carry write-path instructions that no longer "
        "match the runtime. Edit the lines above — the correct form is relative "
        "to your state root, with no `tesseract/` prefix."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
