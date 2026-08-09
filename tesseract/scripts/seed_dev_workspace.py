"""Seed a dev checkout's `tesseract/workspace/` from `_shipping/`.

`ensure_workspace_seeded` cannot do this. It seeds `workspace_dir()` from the
app tree's `tesseract/workspace/`, and on a dev checkout those two paths are
the same directory — `_seed_from_templates` returns early on exactly that
case, so a dev tree never gets the documents. In a production tree the two
differ, because `build_shipping_workspace` has already rendered `_shipping/`
into `tesseract/workspace/` at build time and `workspace_dir()` resolves under
`TESSERACT_HOME`.

The result is that `PROPOSABLE_PATHS` names twelve documents that do not exist
on a dev machine, so the Identity tab reports them all missing and
`propose_change` is inert (`validate_target` refuses a target that is absent).
This script closes that gap by doing on dev what the build does for
production: render `_shipping/` with the configured identity.

Seeding is one-way by design. `seed_tree` never overwrites a file that already
exists and never re-renders one after a rename, because prose the operator has
since edited must not be rewritten under them. So the name in `mirror.yaml` at
the moment this runs is the name that stays in the documents.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tesseract.config_seed import identity_values, seed_tree
from tesseract.paths import TESSERACT_DIR, workspace_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be written without writing it.",
    )
    args = parser.parse_args(argv)

    source = TESSERACT_DIR / "workspace" / "_shipping"
    if not source.is_dir():
        print(f"no shipping templates at {source}", file=sys.stderr)
        return 1

    destination = workspace_dir()
    values = identity_values()

    if args.dry_run:
        pending = [
            path.relative_to(source).as_posix()
            for path in sorted(source.rglob("*"))
            if path.is_file() and not (destination / path.relative_to(source)).exists()
        ]
        for rel in pending:
            print(f"would seed {rel}")
        print(f"{len(pending)} file(s) pending, rendered as {values['agent_name']}")
        return 0

    # Deliberately NOT recorded in `runtime/seeded.json`. That manifest is
    # the automatic seeder's memory of "the operator already dealt with
    # this", and writing into it here would claim the boot-time path had
    # run when it had not. A dev checkout is asserted to carry no manifest
    # at all (`test_additive_seeding.py::test_dev_checkout_is_still_a_no_op`),
    # because on dev source and destination are the same tree and seeding
    # is a no-op by construction.
    #
    # The cost is that a document deleted here comes back if this script is
    # re-run. That is the right trade for a manual, explicit command: asking
    # for the seed again should restore what is missing.
    added = seed_tree(source, destination, values)
    for rel in added:
        print(f"seeded {rel}")
    print(f"{len(added)} file(s) seeded into {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
