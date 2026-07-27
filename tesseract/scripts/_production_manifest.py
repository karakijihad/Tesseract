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
    # CLAUDE.md/AGENTS.md document the runtime's internal security
    # architecture (bash_security's check list, permissions model,
    # kernel-lockdown mechanics) — shipping them is information disclosure.
    # The rest is dev clutter with no runtime purpose.
    "CLAUDE.md",
    "AGENTS.md",
    ".github",
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
)

# Created empty (with .gitkeep) so the runtime has its dirs on first boot.
EMPTY_DIRS = (
    "tesseract/memory-store",
    "tesseract/vault",
    "tesseract/logs",
    "tesseract/sessions",
    "tesseract/agenda",
)

# Source dirs whose NAMES collide with state dirs — must survive the build.
# Asserted by the test suite; listed here so the collision stays documented.
MUST_SHIP = (
    "tesseract/mirror/src/components/sessions",
    "tesseract/mirror/src/views/workspace",
    "tesseract/orchestrator/tars_controller",
)
