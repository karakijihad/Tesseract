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
    # Frontend test surface, Python's counterpart to `tesseract/tests` above:
    # e2e specs + their shared fixtures never run outside a dev checkout (no
    # `playwright` browser install ships), and the runner configs they need
    # (`playwright.config.ts`, `vitest.config.ts`) have no other consumer —
    # `release:build` runs `release-gate.mjs && tauri build` only, never
    # `test`/`e2e`. Per-file `*.test.ts(x)` units ship alongside their
    # source today; those are dropped via EXCLUDE_GLOBS below instead, since
    # a path-prefix rule can't reach files interleaved throughout `src/`.
    "tesseract/mirror/e2e",
    "tesseract/mirror/playwright.config.ts",
    "tesseract/mirror/vitest.config.ts",
    # `#[cfg(test)]`-gated in `lib.rs` (`mod test_support;`) — never compiled
    # into a release build, so dropping the file cannot break `cargo build
    # --release` / `tauri build`. Only `cargo test` (never run against the
    # shipped tree) needs it.
    "tesseract/mirror/src-tauri/src/test_support.rs",
    # CLAUDE.md/AGENTS.md document the runtime's internal security
    # architecture (bash_security's check list, permissions model,
    # kernel-lockdown mechanics) — shipping them is information disclosure.
    # The rest is dev clutter with no runtime purpose.
    # The project registry records real filesystem paths, git remotes and
    # project names. It is gitignored, so nothing should reach the manifest —
    # this is the belt to that braces, because a single accidental `git add -f`
    # would ship the operator's directory layout.
    "tesseract/projects",
    # Writes the capability matrix into the docs tree, which production does
    # not have. Its only consumers are CI and a dev test, so shipping it puts
    # a script in a user's tree whose single action is to fail.
    "tesseract/scripts/generate_capability_matrix.py",
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
    # Vitest unit specs live beside the source they cover throughout
    # `tesseract/mirror/src/**`, not under one prefix EXCLUDE_PATHS could
    # name — matched by filename instead, same reasoning as the e2e/
    # exclusion above.
    "*.test.ts",
    "*.test.tsx",
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
    # Task 17: memory-store/vault/workshop ship ready with hand-authored
    # scaffold content (MEMORY.md+WHAT_NOT_TO_SAVE.md, CATALOG.md,
    # INDEX.md+README.md), same pattern as workspace above — templates live
    # under each dir's `_shipping/`, applied via
    # make_shipping_workspace.build_shipping_workspace, and must not also
    # appear as a `_shipping/` subfolder in the raw tracked-file copy.
    "tesseract/memory-store/*.md",
    "tesseract/vault/*.md",
    "tesseract/workshop/*.md",
)

# Nothing. The shipped tree is `app/` — sealed, replaced wholesale by every
# update — and no state may live in it. State directories are created under
# `home/` and `runtime/` at boot instead, which is also the only place they
# survive an update.
#
# memory-store/vault/workshop were never here either: they ship real
# scaffold content (see EXCLUDE_PATH_GLOBS above + build_production_tree.build)
# which phase 5's additive seeding copies into `home/`.
EMPTY_DIRS: tuple[str, ...] = ()

# Source dirs whose NAMES collide with state dirs — must survive the build.
# Asserted by the test suite; listed here so the collision stays documented.
MUST_SHIP = (
    "tesseract/mirror/src/components/sessions",
    "tesseract/mirror/src/views/workspace",
    "tesseract/orchestrator/agent_controller",
)
