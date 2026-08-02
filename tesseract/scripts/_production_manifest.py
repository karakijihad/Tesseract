"""What ships in the production tree — single source of truth.

ALLOWLIST, not denylist. The file set comes from `git ls-files` (TRACKED files
only). Anything untracked or gitignored — venvs, node_modules, Rust `target/`,
`uploads/`, `downloads/`, `workers/`, `.claude/`, `Research/`, egg-info, sqlite
indexes, `.env`, every runtime-state dir — is excluded automatically and
permanently, with no manifest to maintain.

Why: a denylist proved impossible to complete. The first dry run against the
real repo leaked the operator's CV (`uploads/channels/telegram/`) and 7.6 GB of
Rust build cache, all of it already gitignored. Tracking status is the correct
authority for "is this operator-private?".

EXCLUDE_PATHS below therefore covers only TRACKED things that must not ship.
"""

from __future__ import annotations

# Repo-root-relative prefixes dropped even though git tracks them (posix).
EXCLUDE_PATHS = (
    "Docs",
    # CL-M7: excluding the whole test tree means the shipped repo carries no
    # PII/secret guard of its own — `tesseract/scripts/audit_release_tree.py`
    # is the standalone replacement that DOES ship (it lives under
    # `tesseract/scripts/`, untouched by this exclusion) and that
    # `build_production_tree.main()` runs against the tree it just built.
    "tesseract/tests",
    # CLAUDE.md/AGENTS.md document the runtime's internal security
    # architecture (bash_security's check list, permissions model,
    # kernel-lockdown mechanics) — shipping them is information disclosure.
    # The rest is dev clutter with no runtime purpose.
    "CLAUDE.md",
    "AGENTS.md",
    ".github",
    "pytest.ini",
    ".pre-commit-config.yaml",
)

# File-name globs dropped anywhere (belt-and-braces; git already excludes most).
EXCLUDE_GLOBS = (
    "*.sqlite",
    "*.sqlite-shm",
    "*.sqlite-wal",
    "*.pyc",
    ".env",
    "trusted_dirs.json",
    ".mcp.json",
    "owners-notes.md",
)

# Full-relative-path globs (matched against the whole posix path, not just
# the filename — unlike EXCLUDE_GLOBS above). Task 8b: config ships from
# hand-authored templates only (tesseract/config/_shipping/*.yaml), never
# from the operator's live tesseract/config/*.yaml. This one pattern
# excludes both the live top-level yaml files AND the _shipping/*.yaml
# templates themselves from the raw tracked-file copy — the templates map
# onto config/*.yaml via make_shipping_config.build_shipping_config, they
# never appear as their own tesseract/config/_shipping/ subfolder in the
# output tree.
EXCLUDE_PATH_GLOBS = (
    "tesseract/config/*.yaml",
    # Task 11f: workspace ships from hand-authored templates only
    # (tesseract/workspace/_shipping/*.md), never the operator's live
    # tesseract/workspace/ (fully gitignored, private). fnmatch's `*`
    # matches across `/`, so this one pattern excludes both a live
    # tesseract/workspace/*.md (there shouldn't be any tracked) AND the
    # _shipping/*.md templates themselves from the raw tracked-file copy —
    # they map onto tesseract/workspace/*.md via
    # make_shipping_workspace.build_shipping_workspace instead.
    "tesseract/workspace/*.md",
    # Task 17: memory-store/vault/tars-workshop ship ready with hand-authored
    # scaffold content (MEMORY.md+WHAT_NOT_TO_SAVE.md, CATALOG.md,
    # INDEX.md+README.md), same pattern as workspace above — templates live
    # under each dir's `_shipping/`, applied via
    # make_shipping_workspace.build_shipping_workspace, and must not also
    # appear as a `_shipping/` subfolder in the raw tracked-file copy.
    "tesseract/memory-store/*.md",
    "tesseract/vault/*.md",
    "tesseract/tars-workshop/*.md",
)

# Nothing. The shipped tree is `app/` — sealed, replaced wholesale by every
# update — and no state may live in it. State directories are created under
# `home/` and `runtime/` at boot instead, which is also the only place they
# survive an update.
#
# memory-store/vault/tars-workshop were never here either: they ship real
# scaffold content (see EXCLUDE_PATH_GLOBS above + build_production_tree.build)
# which phase 5's additive seeding copies into `home/`.
EMPTY_DIRS: tuple[str, ...] = ()

# Source dirs whose NAMES collide with state dirs — must survive the build.
# Asserted by the test suite; listed here so the collision stays documented.
MUST_SHIP = (
    "tesseract/mirror/src/components/sessions",
    "tesseract/mirror/src/views/workspace",
    "tesseract/orchestrator/tars_controller",
)
