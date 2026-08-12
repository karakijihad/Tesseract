"""Standalone release-tree audit — CL-M7.

`build_production_tree.py` ships every tracked file EXCEPT `tesseract/tests`
(see `_production_manifest.py`), so a friend's clone of the production repo
previously carried zero PII/secret guard of its own — the pytest suite that
checks this never ships. This script closes that gap: it walks a directory
tree, flags generic secret/PII shapes, and exits non-zero on a hit. It ships
INTO the production tree (unlike the test suite) and needs no pytest or
dev-only dependency — stdlib plus `tesseract.lib` only.

Two further checks run alongside the PII/secret scan, both structural rather
than pattern-on-content: `scan_test_surface` fails on any test-only file
(any language) reaching the built tree, and `scan_work_notes` fails on
provenance-only comment fragments (dated audit references, `*-report.md`
citations, hyphenated task-id citations, session-log paths) — the operator's
standard is that shipped comments explain the *why* that still matters, not
who found it or when. Both are independent of the optional tokens file below
and always run — `scan_all` is the composed entry point both this module's
`main` and `build_production_tree.main` call.

The operator-specific token denylist
(`.pii-tokens.local.json`, see
`tests/distributable_app/test_no_pii_in_production_tree.py`) stays external,
gitignored, and OPTIONAL — this script runs a full generic scan without it
and only additionally checks it when `--tokens-file` points at one. Neither
new check reads it: both are effective with zero configuration, by design.

`build_production_tree.main()` runs this against the tree it just generated
and fails the build on a hit.
"""

from __future__ import annotations

import argparse
import codecs
import fnmatch
import json
import re
import sys
from pathlib import Path

from tesseract.lib.pii_patterns import EMAIL_RE, WINDOWS_USER_PATH_RE
from tesseract.lib.secret_patterns import CREDENTIAL_PATTERNS

# `/home/<name>` — POSIX counterpart to WINDOWS_USER_PATH_RE. Placeholders
# (`<user>`, `$USER`, `...`) are excluded, same shape as the Windows pattern.
POSIX_HOME_PATH_RE = re.compile(r"/home/(?!<|\$|\.\.\.)[A-Za-z0-9._-]+")

# Filenames that mark a file as test-only regardless of which directory it
# ships under — Vitest/Playwright specs live beside the source they cover
# throughout `tesseract/mirror/src/**`, so a directory-prefix rule alone
# (below) cannot reach them.
_TEST_SURFACE_NAME_GLOBS: tuple[str, ...] = (
    "test_*.py",
    "*_test.py",
    "conftest.py",
    "*.test.ts",
    "*.test.tsx",
    "*.spec.ts",
    "*.spec.tsx",
    "pytest.ini",
    "playwright.config.ts",
    "vitest.config.ts",
    "test_support.rs",
)

# Directory names that are test infrastructure end to end — anything found
# under one of these, regardless of filename, is test surface.
_TEST_SURFACE_DIR_NAMES: frozenset[str] = frozenset({"tests", "e2e", "__tests__"})

# Provenance-only comment fragments: a citation to a specific dated audit
# pass, a specific numbered task, a specific report file, or a specific
# session log — useful to whoever was in the room when the change landed,
# meaningless (or actively misleading, since none of these ship) to anyone
# reading the installed app. The technical warning a comment carries must
# survive rewriting; only the citation is disallowed.
_WORK_NOTE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"audit-\d{4}-\d{2}-\d{2}"),
    re.compile(r"\btask-\d+\b"),
    re.compile(r"[\w.-]+-report\.md"),
    re.compile(r"Docs/Sessions/\d{4}-\d{2}-\d{2}\.md"),
    re.compile(r"\.superpowers/"),
)

# This module declares those patterns, so it necessarily contains them —
# `\.superpowers/` matches its own source literal. It ships (everything under
# `scripts/` does), so scanning it finds only its own denylist and fails every
# build. Skipped by name rather than by an inline marker, because a marker
# would be one more thing a pattern could match.
_SELF_RELATIVE = Path("tesseract") / "scripts" / Path(__file__).name

# References to things that exist only in the development repository. A reader
# of the public tree cannot follow any of them: the docs tree, the agent
# instruction files and the test suite are all excluded from the build by
# `_production_manifest`, and a dated session id names a conversation nobody
# outside the project ever had.
#
# This is a different failure from `_WORK_NOTE_PATTERNS` above, which catches
# provenance *attribution*. These catch a pointer — a sentence that reads as
# helpful ("see `CLAUDE.md` for the authoritative notes") and sends a stranger
# to a file that is not there. A per-directory `README.md` written for us
# rather than for them is the same defect at paragraph scale.
# `AGENTS.md` is deliberately NOT here. It is a real filename this product
# writes and reads — the assistant's own `workspace/AGENTS.md`, and the
# conventions file `project_new` seeds into a user's repository. Flagging it
# would be flagging the product.
# Named so the allowlist below can exempt ONE pattern in a file rather than
# the whole file — `config/permissions.yaml` legitimately names a test path in
# a policy rule AND illegitimately cited the dev instruction file, and a
# per-file exemption would have hidden the second while excusing the first.
_INTERNAL_REF_PATTERNS: dict[str, re.Pattern[str]] = {
    # Capital D: `Docs/` is this project's tree. Lowercase `docs/` is left
    # alone because it is how the rest of the world spells a documentation URL.
    "docs-tree": re.compile(r"\bDocs[/\\][A-Za-z]"),
    "claude-md": re.compile(r"\bCLAUDE\.md\b"),
    "test-tree": re.compile(r"\btesseract[/\\]tests\b"),
    # A session id, as cited in comments: `session 2026-07-12-1818`.
    "session-id": re.compile(r"\bsession \d{4}-\d{2}-\d{2}"),
}

# Where a pattern names a VALUE rather than citing an authority: code that
# detects, writes, or excludes a file by that name, and policy rules scoped to
# a path. Allowlisted explicitly because the distinction is semantic and no
# regex draws it — "per CLAUDE.md hard rule" and
# `_CONVENTION_FILES = ("AGENTS.md", "CLAUDE.md", ...)` are the same characters
# doing opposite jobs.
#
# The cost is that a genuine citation added to one of these files, matching
# that same pattern, slips through. Kept narrow to keep that cost small: the
# path must be exact and the pattern must be named.
_INTERNAL_REF_ALLOWED: dict[str, frozenset[str]] = {
    # Detects a repository's conventions file by name.
    "tesseract/orchestrator/projects/detect.py": frozenset({"claude-md"}),
    # Names what memory must not store: the always-loaded instruction file.
    "tesseract/memory/what_not_to_save.py": frozenset({"claude-md"}),
    "tesseract/memory-store/WHAT_NOT_TO_SAVE.md": frozenset({"claude-md"}),
    # The build itself: names the paths it excludes, then generates a fresh
    # instruction file. Every match here is a path being acted on.
    "tesseract/scripts/_production_manifest.py": frozenset({"claude-md", "test-tree"}),
    "tesseract/scripts/build_production_tree.py": frozenset({"claude-md", "test-tree"}),
    "tesseract/scripts/check_tree_invariants.py": frozenset({"claude-md", "test-tree"}),
    # The prompt manifest lists the capability matrix as a readable pointer.
    # `_pointer_exists` drops it when the file is absent, which is every
    # production install — so the entry is dev-only by construction, not a
    # dangling instruction.
    "tesseract/brain/prompt_content.py": frozenset({"docs-tree"}),
    # Names the generated instruction files the sealed-tree check exempts.
    "tesseract/permissions/bash_security.py": frozenset({"claude-md"}),
    # Product documentation about the coding CLIs and their init commands.
    "tesseract/agents/cli-reference.md": frozenset({"claude-md"}),
    # A path-scoped policy rule and the code reading it. The path is the value
    # being matched at runtime, not a pointer for a reader to follow.
    "tesseract/config/permissions.yaml": frozenset({"test-tree"}),
    "tesseract/permissions/policy.py": frozenset({"test-tree"}),
    "tesseract/permissions/readonly_commands.py": frozenset({"test-tree"}),
    # A build-tool exclude glob.
    "tesseract/mirror/vite.config.ts": frozenset({"test-tree"}),
}

_GENERIC_PATTERNS: tuple[re.Pattern[str], ...] = (
    EMAIL_RE,
    WINDOWS_USER_PATH_RE,
    POSIX_HOME_PATH_RE,
    *CREDENTIAL_PATTERNS,
)

# Known-synthetic literals that legitimately ship (mirrors the dev-only
# `_KNOWN_SYNTHETIC_FIXTURE_VALUES` allowlist in
# `tests/distributable_app/test_no_generic_pii_in_production_tree.py`, kept
# separately since this script must stay standalone/importable with no
# `tesseract/tests` dependency — that whole tree is excluded from the
# production build). Exact-value only, never a pattern loosening.
_KNOWN_SYNTHETIC_LITERALS = frozenset(
    {
        # A fake token in a URL userinfo segment, used by two `#[cfg(test)]`
        # modules that ship inside their source files: `provision.rs` proves
        # the Rust credential scrubber redacts it, and `exe_update.rs` proves
        # `owner_repo` parses past userinfo instead of refusing the URL. The
        # email-shaped pattern fires on the `<token>@github.com` run. One
        # literal serves both so this allowlist does not grow per test.
        "ghp_supersecret@github.com",
        # tesseract/mirror/src-tauri/src/provision.rs — a second synthetic
        # credentialed URL, proving the scrubber also fires on streamed tool
        # output. `example.invalid` is RFC 2606 reserved and can never resolve.
        # The dev-side test allowlist has carried this since the fixture landed;
        # this copy was missing it, which failed the build rather than the test.
        "secret@example.invalid",
        # tesseract/mirror/{e2e,src}/**/*.test.ts(x) — synthetic POSIX
        # fixture username ("op") used in controller-transcript-path tests,
        # not a real operator home directory.
        "/home/op",
    }
)


def _load_optional_tokens(tokens_file: Path | None) -> tuple[str, ...]:
    if tokens_file is None or not tokens_file.is_file():
        return ()
    data = json.loads(tokens_file.read_text(encoding="utf-8"))
    return tuple(data.get("tokens", ()))


def _decode_text(raw: bytes) -> str | None:
    """Decode `raw` as text, or return None if it is genuinely binary.

    A NUL byte alone does not mean binary: UTF-16 puts one beside every ASCII
    character, so a plain `b"\\x00" in raw` skip silently exempted every
    UTF-16 file from the scan — a real gap on Windows, where hand-authored and
    exported text is often UTF-16LE. A leaked key in such a file would have
    passed the release gate unnoticed.

    BOM-marked UTF-16 is decoded explicitly. Unmarked content is then probed
    for the alternating-NUL pattern UTF-16 produces for ASCII-range text, so a
    BOM-less export is still scanned. Anything left holding NULs is treated as
    binary, as before.
    """
    for bom, encoding in ((codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")):
        if raw.startswith(bom):
            return raw[len(bom):].decode(encoding, errors="ignore")
    if raw.startswith(codecs.BOM_UTF8):
        return raw[len(codecs.BOM_UTF8):].decode("utf-8", errors="ignore")

    if b"\x00" not in raw:
        return raw.decode("utf-8", errors="ignore")

    # No BOM but NUL-bearing: distinguish BOM-less UTF-16 from a real binary by
    # checking which byte position the NULs occupy. ASCII text in UTF-16LE has
    # them on odd offsets, UTF-16BE on even ones.
    sample = raw[: 4096 - (4096 % 2)]
    if len(sample) >= 2:
        odd_nuls = sample[1::2].count(0)
        even_nuls = sample[0::2].count(0)
        half = len(sample) // 2
        if odd_nuls >= half * 0.9 and even_nuls == 0:
            return raw.decode("utf-16-le", errors="ignore")
        if even_nuls >= half * 0.9 and odd_nuls == 0:
            return raw.decode("utf-16-be", errors="ignore")
    return None


def scan(root: Path, tokens_file: Path | None = None) -> list[str]:
    """Return one offender string per hit; empty list means clean."""
    tokens = _load_optional_tokens(tokens_file)
    offenders: list[str] = []
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        body = _decode_text(raw)
        if body is None:
            continue  # genuinely binary asset — not a text scan target
        rel = path.relative_to(root)
        for pattern in _GENERIC_PATTERNS:
            for hit in pattern.findall(body):
                value = hit if isinstance(hit, str) else hit[0]
                if value in _KNOWN_SYNTHETIC_LITERALS:
                    continue
                offenders.append(f"{rel}: {value}")
        lowered = body.lower()
        for tok in tokens:
            if tok.lower() in lowered:
                offenders.append(f"{rel}: operator token '{tok}'")
    return offenders


def scan_test_surface(root: Path) -> list[str]:
    """Return one offender string per test-only file found under `root`;
    empty list means none shipped.

    Two passes, independent of filesystem walk order: first every
    test-infrastructure directory is collected, then every file is checked
    against that set (so a file inside one is reported once, via its
    directory, not once per file) and against the filename globs.
    """
    test_dirs: set[Path] = {
        p.relative_to(root)
        for p in root.rglob("*")
        if p.is_dir() and not p.is_symlink() and p.name in _TEST_SURFACE_DIR_NAMES
    }
    offenders: list[str] = [f"{d}/: test-surface directory shipped" for d in sorted(test_dirs)]
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(d in rel.parents for d in test_dirs):
            continue  # already flagged via its containing directory
        if any(fnmatch.fnmatch(path.name, pat) for pat in _TEST_SURFACE_NAME_GLOBS):
            offenders.append(f"{rel}: test-surface file shipped")
    return offenders


def scan_work_notes(root: Path) -> list[str]:
    """Return one offender string per work-note comment fragment found under
    `root`; empty list means none shipped. See `_WORK_NOTE_PATTERNS` for
    what counts — provenance citations, not the technical content beside
    them, which this does not touch and does not need to understand.
    """
    offenders: list[str] = []
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        body = _decode_text(raw)
        if body is None:
            continue
        rel = path.relative_to(root)
        if rel == _SELF_RELATIVE:
            continue
        for pattern in _WORK_NOTE_PATTERNS:
            match = pattern.search(body)
            if match:
                offenders.append(f"{rel}: work-note comment pattern {match.group(0)!r}")
    return offenders


def scan_internal_refs(root: Path) -> list[str]:
    """Return one offender string per dangling internal reference found under
    `root`; empty list means none shipped. See `_INTERNAL_REF_PATTERNS`.

    Every hit is reported, not just the first per file: these arrive in
    clusters (one README can carry a dozen), and fixing them one build at a
    time is the slow way to do it.
    """
    offenders: list[str] = []
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        body = _decode_text(raw)
        if body is None:
            continue
        rel = path.relative_to(root)
        if rel == _SELF_RELATIVE:
            continue
        allowed = _INTERNAL_REF_ALLOWED.get(rel.as_posix(), frozenset())
        for name, pattern in _INTERNAL_REF_PATTERNS.items():
            if name in allowed:
                continue
            for match in dict.fromkeys(pattern.findall(body)):
                offenders.append(f"{rel}: internal reference {match!r} ({name})")
    return offenders


def scan_all(root: Path, tokens_file: Path | None = None) -> list[str]:
    """Every check this module runs, composed. The single entry point both
    this module's CLI and `build_production_tree.main()` call — a caller
    reaching for just `scan()` gets only the PII/secret half by construction,
    so anything that must run in the real release gate belongs here instead.
    """
    return [
        *scan(root, tokens_file),
        *scan_test_surface(root),
        *scan_work_notes(root),
        *scan_internal_refs(root),
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tesseract.scripts.audit_release_tree")
    ap.add_argument("tree_root", type=Path)
    ap.add_argument(
        "--tokens-file",
        type=Path,
        default=None,
        help="optional operator-specific denylist JSON ({'tokens': [...]}); never shipped, never required",
    )
    args = ap.parse_args(argv)

    offenders = scan_all(args.tree_root, args.tokens_file)
    if offenders:
        print("audit_release_tree: release-tree violation(s) found:", file=sys.stderr)
        for offender in offenders:
            print(f"  {offender}", file=sys.stderr)
        return 1
    print(f"audit_release_tree: clean ({args.tree_root})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
