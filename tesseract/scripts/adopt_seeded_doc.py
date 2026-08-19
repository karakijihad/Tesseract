"""Hand a seeded document back to the product after correcting it in place.

`refresh_seeded_docs` decides ownership by comparing a document against the
digest recorded when it was seeded: matching means the product's, differing
means the assistant's or the operator's. That is the right rule, and it has one
sharp edge — **editing an install's product document by hand permanently opts
it out of updates.** The bytes stop matching, the file reads as authored prose,
and no later correction is ever delivered to it again.

That is not hypothetical. `workspace/TOOLS.md` on this machine was corrected by
hand while closing a phase, exactly as the phase instructed, and silently left
the refresh path in the same motion.

This is the other half of that method: re-record the digest so a document you
have brought *to* the shipped text is treated as shipped text again.

**It hands ownership away**, so it refuses everything it cannot prove is safe:

- a document in a tree nothing refreshes (only `workspace/` is wired), where
  re-recording the digest would change nothing and claim otherwise;
- a document reached through a symlink, or resolving outside the state root;
- a document whose contents do NOT match the template it would be refreshed
  from — the case the docstring above describes is the *only* safe one, and
  before this check it was the operator's job to remember that;
- `SOUL.md`, `DIARY.md`, named explicitly on top of the content
  check because adopting one tells the next release it may overwrite an
  identity.

`--force` overrides the last two. Nothing overrides the first two.

    python -m tesseract.scripts.adopt_seeded_doc workspace/OPERATING.md
    python -m tesseract.scripts.adopt_seeded_doc --list
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tesseract.config_seed import (
    digest_text,
    identity_values,
    is_safe_seed_target,
    load_seeded,
    load_seeded_digests,
    record_seeded_digests,
    render_placeholders,
)

#: Named on top of the content check, not instead of it. These are the
#: documents the assistant rewrites as it grows, and adopting one is the single
#: irreversible thing this module can do.
AUTHORED = frozenset({"SOUL.md", "DIARY.md"})

#: State trees whose documents a release actually revisits. Only
#: `ensure_workspace_seeded` calls `refresh_seeded_docs`; `memory-store`,
#: `vault` and `workshop` seed additively and are never refreshed, so
#: re-recording a digest there buys nothing and would report that it did.
REFRESHED_TREES = frozenset({"workspace"})


def _state(key: str, home: Path, digests: dict[str, str]) -> str:
    recorded = digests.get(key)
    target = home / key
    if not target.is_file():
        return "missing"
    if target.is_symlink():
        return "symlink"
    if recorded is None:
        return "no recorded digest"
    try:
        current = target.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return "unreadable"
    return "untouched" if digest_text(current) == recorded else "edited"


def _template_text(key: str) -> tuple[str | None, str]:
    """The shipped text `key` would be refreshed from, rendered as it would be.

    Returns `(text, problem)`. The two failures are reported apart because they
    send an operator to different places: an absent template is a broken app
    tree, while a render failure is almost always `mirror.yaml` missing an
    identity key — and calling the second one "no shipped template" points them
    at a file the product plainly ships.
    """
    from tesseract.paths import TESSERACT_DIR

    template = TESSERACT_DIR / key
    if not template.is_file():
        return None, f"no shipped template at {template}"
    try:
        return (
            render_placeholders(template.read_text(encoding="utf-8"), identity_values()),
            "",
        )
    except (OSError, ValueError) as exc:
        return None, f"its shipped template could not be read ({exc})"
    except RuntimeError as exc:
        return None, f"the template could not be rendered ({exc})"


def _print_listing(home: Path) -> int:
    digests = load_seeded_digests()
    seeded = sorted(k for k in load_seeded() if k.endswith(".md"))
    if not seeded:
        print("nothing seeded on this install.")
        return 0
    for key in seeded:
        notes = []
        if Path(key).name in AUTHORED:
            notes.append("assistant-authored")
        if Path(key).parts[0] not in REFRESHED_TREES:
            notes.append("never refreshed")
        suffix = f"  ({', '.join(notes)})" if notes else ""
        print(f"  {_state(key, home, digests):<20} {key}{suffix}")
    print(
        "\n'edited' means the file will never receive another shipped correction. "
        "That is correct for authored prose and wrong for a product document you "
        "fixed by hand. 'never refreshed' means no release revisits that tree at "
        "all, so adopting it would change nothing."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="home-relative, e.g. workspace/TOOLS.md")
    parser.add_argument("--list", action="store_true", help="show every seeded document's state")
    parser.add_argument(
        "--force",
        action="store_true",
        help="adopt a document whose contents differ from the shipped template, "
        f"or one of {', '.join(sorted(AUTHORED))} — this permits a future "
        "release to overwrite it",
    )
    args = parser.parse_args(argv)

    from tesseract.paths import home_dir

    home = home_dir()
    if args.list:
        return _print_listing(home)
    if not args.paths:
        parser.error("name at least one document, or pass --list")

    seeded = load_seeded()
    digests = load_seeded_digests()
    # Every path is judged before anything is printed or written. The write is
    # one call at the end, so printing per path as it cleared would announce
    # adoptions that a later refusal then cancels.
    planned: list[tuple[str, str, str]] = []
    for raw in args.paths:
        key = Path(raw).as_posix()
        target = home / key
        if key not in seeded:
            print(f"[skip] {key} - not a seeded document on this install", file=sys.stderr)
            return 2
        if Path(key).parts[0] not in REFRESHED_TREES:
            print(
                f"[skip] {key} - nothing refreshes that tree, so adopting it would "
                "record a digest no release ever reads",
                file=sys.stderr,
            )
            return 2
        if not is_safe_seed_target(target, home):
            print(
                f"[skip] {key} - not a regular file inside {home}, or reached "
                "through a symlink",
                file=sys.stderr,
            )
            return 2
        try:
            current = target.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            # ValueError covers UnicodeDecodeError — a document re-saved by an
            # editor in the machine's legacy codepage. `refresh_seeded_docs`
            # treats that as untouchable rather than raising; every other
            # refusal here prints a reason, and a traceback is the one an
            # operator is least equipped to read.
            print(f"[skip] {key} - could not be read ({exc})", file=sys.stderr)
            return 2
        shipped, problem = _template_text(key)
        if shipped is None:
            print(f"[skip] {key} - {problem}", file=sys.stderr)
            return 2
        # Named documents first. An authored document that has been rewritten
        # also fails the content check, and "does not match the template" is
        # the weaker reason to give for SOUL.md — it describes the symptom
        # where the other names the stake.
        if Path(key).name in AUTHORED and not args.force:
            print(
                f"[refused] {key} is assistant-authored. Adopting it lets a future "
                "release overwrite it. Pass --force if that is genuinely what you want.",
                file=sys.stderr,
            )
            return 2
        if current != shipped and not args.force:
            print(
                f"[refused] {key} does not match the template it would be "
                "refreshed from, so adopting it would mark content the product "
                "never wrote as replaceable. Bring it to the shipped text first, "
                "or pass --force if you mean it.",
                file=sys.stderr,
            )
            return 2
        planned.append((key, _state(key, home, digests), digest_text(current)))

    record_seeded_digests({key: digest for key, _, digest in planned})
    for key, before, _ in planned:
        print(f"[adopt] {key}: {before} -> untouched (updates will reach it again)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
