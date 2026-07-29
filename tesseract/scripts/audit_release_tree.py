"""Standalone release-tree audit — CL-M7.

`build_production_tree.py` ships every tracked file EXCEPT `tesseract/tests`
(see `_production_manifest.py`), so a friend's clone of the production repo
previously carried zero PII/secret guard of its own — the pytest suite that
checks this never ships. This script closes that gap: it walks a directory
tree, flags generic secret/PII shapes, and exits non-zero on a hit. It ships
INTO the production tree (unlike the test suite) and needs no pytest or
dev-only dependency — stdlib plus `tesseract.lib` only.

The operator-specific token denylist
(`.pii-tokens.local.json`, see
`tests/distributable_app/test_no_pii_in_production_tree.py`) stays external,
gitignored, and OPTIONAL — this script runs a full generic scan without it
and only additionally checks it when `--tokens-file` points at one.

`build_production_tree.main()` runs this against the tree it just generated
and fails the build on a hit.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from tesseract.lib.pii_patterns import EMAIL_RE, WINDOWS_USER_PATH_RE
from tesseract.lib.secret_patterns import CREDENTIAL_PATTERNS

# `/home/<name>` — POSIX counterpart to WINDOWS_USER_PATH_RE. Placeholders
# (`<user>`, `$USER`, `...`) are excluded, same shape as the Windows pattern.
POSIX_HOME_PATH_RE = re.compile(r"/home/(?!<|\$|\.\.\.)[A-Za-z0-9._-]+")

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
        # tesseract/mirror/src-tauri/src/provision.rs — a fake token embedded
        # in a sample clone-error string to prove the Rust-side credential
        # scrubber redacts it; the email-shaped pattern fires on the
        # `<token>@github.com` userinfo run.
        "ghp_supersecret@github.com",
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
        if b"\x00" in raw:
            continue  # binary asset — not a text scan target
        body = raw.decode("utf-8", errors="ignore")
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

    offenders = scan(args.tree_root, args.tokens_file)
    if offenders:
        print("audit_release_tree: PII/secret pattern found:", file=sys.stderr)
        for offender in offenders:
            print(f"  {offender}", file=sys.stderr)
        return 1
    print(f"audit_release_tree: clean ({args.tree_root})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
