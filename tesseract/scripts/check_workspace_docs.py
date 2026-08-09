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


def _stale_lines(text: str) -> list[tuple[int, str]]:
    return [
        (number, line.strip())
        for number, line in enumerate(text.splitlines(), start=1)
        if _STALE_WRITE_PATH.search(line)
    ]


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
