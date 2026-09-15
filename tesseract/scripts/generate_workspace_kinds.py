"""Write the frontend's copy of `workspace_events/events.py::ANSWERABLE_WITH`.

What a workspace card kind can be answered with used to be three hand-kept
lists — one in the route, one in the Inbox panel, one implied by the backend
declaration itself — and being absent from one was silent. The
Python side collapsed to one dict; this script is how the TypeScript side
stops being a second, hand-typed opinion about the same fact.

    python -m tesseract.scripts.generate_workspace_kinds --check   # exit 1 on drift
    python -m tesseract.scripts.generate_workspace_kinds --write
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tesseract.paths import TESSERACT_DIR
from tesseract.workspace_events.events import ANSWERABLE_WITH

TS_PATH = (
    TESSERACT_DIR / "mirror" / "src" / "stores" / "workspaceKinds.generated.ts"
)

_BANNER = """\
// GENERATED — do not hand-edit. Source of truth:
// `tesseract/workspace_events/events.py::ANSWERABLE_WITH`.
//
// What a workspace card kind can be answered with: 'approve'/'reject' for a
// decision, 'resolve' for a report. Regenerate after `ANSWERABLE_WITH`
// changes:
//   python -m tesseract.scripts.generate_workspace_kinds --write
// Check for drift:
//   python -m tesseract.scripts.generate_workspace_kinds --check

import type { EventKind } from './workspace';
"""

_IS_ACTIONABLE_FN = """\
/** Whether the Inbox draws Approve/Reject for a card of this kind, rather
 *  than a plain Resolve. `resolve` wins when a kind carries both:
 *  `clarification` and `nudge` are backend-decidable (they count as
 *  "waiting on you" for `/queue`, the notifier, the return note), but the
 *  operator's real answer is a comment and the row draws only Resolve —
 *  `orchestrator/recovery/effects.py::build_card` files a `clarification`
 *  specifically because it "does not offer a button that runs anything".
 *  A kind that is only decidable (no `resolve` in its verbs) draws
 *  Approve/Reject as it always has. */
export function isActionable(kind: EventKind): boolean {
  const verbs = ANSWERABLE_WITH[kind] ?? [];
  return verbs.includes('approve') && !verbs.includes('resolve');
}\
"""


def render(answerable_with: dict[str, tuple[str, ...]]) -> str:
    """The whole file, deterministic for a given mapping."""
    lines: list[str] = [_BANNER, "export const ANSWERABLE_WITH: Record<EventKind, readonly string[]> = {"]
    for kind, verbs in answerable_with.items():
        verb_list = ", ".join(f"'{v}'" for v in verbs)
        lines.append(f"  {kind}: [{verb_list}],")
    lines.append("};")
    lines.append("")
    lines.append(_IS_ACTIONABLE_FN)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the file")
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if the file is not what this would write"
    )
    args = parser.parse_args(argv)

    wanted = render(ANSWERABLE_WITH)

    if args.check:
        current = TS_PATH.read_text(encoding="utf-8") if TS_PATH.is_file() else ""
        if current == wanted:
            print(f"[ok] {TS_PATH}")
            return 0
        print(f"[drift] {TS_PATH} is not what ANSWERABLE_WITH would write")
        print("       run: python -m tesseract.scripts.generate_workspace_kinds --write")
        return 1

    if not args.write:
        sys.stdout.write(wanted)
        return 0

    TS_PATH.write_text(wanted, encoding="utf-8")
    print(f"  wrote {TS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
