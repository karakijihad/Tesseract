"""Plan 2c Task 5c — build-time guard: the shipped production tree must never
carry operator-specific PII.

Builds the REAL repo (git-tracked files only, same as the shipping build) into
a scratch dir and scans every shipped file for a denylist of operator
identifiers. The denylist itself now lives OUTSIDE this file, in
`<repo root>/.pii-tokens.local.json` (gitignored, operator-machine only) —
this test file DOES ship as of Task 8 (tests are no longer excluded from the
production tree), so the tokens must never be hardcoded here again. On a
friend's clone the token file is absent and this module is skipped loudly;
`test_no_generic_pii_in_production_tree.py` is the guard that still runs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tesseract.paths import ROOT
from tesseract.scripts.build_production_tree import build

_TOKENS_FILE = ROOT / ".pii-tokens.local.json"
if not _TOKENS_FILE.exists():
    pytest.skip(
        "PII token list absent — operator-machine check SKIPPED (generic guard still ran)",
        allow_module_level=True,
    )
OPERATOR_PII_TOKENS = tuple(json.loads(_TOKENS_FILE.read_text(encoding="utf-8"))["tokens"])

# The production repo stays under the operator's personal GitHub account
# (operator decision 2026-07-27; an org is deferred). Its handle is a
# substring of the operator's name, so the URL itself would otherwise trip
# this guard in `tesseract/mirror/src-tauri/src/repo.rs::DEFAULT_REPO_URL`
# and the `provision.rs` scrubber tests that embed the same URL in sample
# error strings. These exact strings are stripped from a file's body BEFORE
# the token scan below — nothing else is exempted, so the operator's name
# appearing anywhere else in the tree still fails the guard.
_ALLOWED_OPERATOR_URL_STRINGS = (
    "https://github.com/karakijihad/Tesseract.git",
    "karakijihad/Tesseract",
)


def _strip_allowed_operator_urls(body: str) -> str:
    for allowed in _ALLOWED_OPERATOR_URL_STRINGS:
        body = re.sub(re.escape(allowed), "", body, flags=re.IGNORECASE)
    return body


def test_production_tree_has_no_operator_pii(tmp_path: Path) -> None:
    out = tmp_path / "prod"
    build(ROOT, out)  # real git-tracked build — no fixture file list

    offenders = []
    for p in out.rglob("*"):
        if not p.is_file():
            continue
        try:
            body = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lowered = _strip_allowed_operator_urls(body).lower()
        for tok in OPERATOR_PII_TOKENS:
            if tok.lower() in lowered:
                offenders.append(f"{tok} -> {p.relative_to(out)}")

    assert not offenders, "operator PII in production tree:\n" + "\n".join(offenders)


def test_allowed_repo_url_does_not_blanket_exempt_operator_name() -> None:
    """The canonical-URL allowance must stay narrow: it strips only the exact
    allowed strings, not the operator's name wherever it appears. Feed the
    same scan logic a synthetic body containing the allowed URL plus one
    operator token placed somewhere else entirely, and confirm that
    non-URL occurrence still trips the guard.

    The token is read from OPERATOR_PII_TOKENS at runtime rather than
    hardcoded here, so this file — which ships in the production tree —
    never itself carries an unbroken copy of the operator's name outside
    the allowed URL strings above.
    """
    outside_token = OPERATOR_PII_TOKENS[0]
    synthetic = (
        "the canonical clone target is https://github.com/karakijihad/Tesseract.git "
        f"but this unrelated line about {outside_token} is not the repo url"
    )
    lowered = _strip_allowed_operator_urls(synthetic).lower()
    hits = [tok for tok in OPERATOR_PII_TOKENS if tok.lower() in lowered]
    assert hits, "operator token outside the allowed URL must still be reported by the guard"
