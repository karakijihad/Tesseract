"""Write `workspace/skills/carried.txt` from the playbooks on this machine.

The twin of `generate_working_set`, for the other thing a turn carries. The
names in the file are the operator's choice and this script preserves them
exactly; the annotation beside each one and the list of what is not carried
are read off the playbooks every time it runs, so the file cannot describe a
set that is no longer there.

Unlike the tool half, this reads and writes a file that never ships. Playbooks
are written out of one operator's work and `tesseract/workspace/` is private
per machine, so `--check` here guards the operator's own file against going
stale after they write a playbook, rather than guarding a tracked file against
a release.

Usage:
    python -m tesseract.scripts.generate_playbook_set --check   # exit 1 on drift
    python -m tesseract.scripts.generate_playbook_set --write
"""

from __future__ import annotations

import argparse
import sys

from tesseract.brain.playbook_set import carried_path, load_carried_names, render
from tesseract.brain.skills import load_skills


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the file")
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if the file is not what this would write"
    )
    args = parser.parse_args(argv)

    path = carried_path()
    wanted = render(sorted(load_carried_names(path)), load_skills(path.parent))

    if args.check:
        current = path.read_text(encoding="utf-8") if path.is_file() else ""
        if current == wanted:
            print(f"[ok] {path}")
            return 0
        print(f"[drift] {path} is not what the playbooks would write")
        print("       run: python -m tesseract.scripts.generate_playbook_set --write")
        return 1

    if not args.write:
        sys.stdout.write(wanted)
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(wanted, encoding="utf-8")
    print(f"  wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
