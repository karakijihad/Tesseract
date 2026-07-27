"""Task 8 — always-on PII/secret guard, no token list required.

`test_no_pii_in_production_tree.py` skips on any machine without
`.pii-tokens.local.json` (every friend's clone, by design). That skip must
never become a silent gate on the whole PII story, so this module lives
separately: a module-level `pytest.skip(allow_module_level=True)` only skips
the tests IN that module, never these. Runs the same build-and-scan shape
against generic patterns that need no operator-specific data — real-looking
email addresses, `C:\\Users\\<name>`-style host paths, and common credential
prefixes.
"""

from __future__ import annotations

import re
from pathlib import Path

from tesseract.paths import ROOT
from tesseract.scripts.build_production_tree import build

# Real email local-part must start at a genuine token boundary (not mid-run,
# not right after "/" — which is how `icons/foo@2x.png` asset filenames and
# `https://token@host` URL user-info segments false-positive) and excludes
# IANA-reserved test domains (RFC 2606) plus GitHub's noreply pattern.
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])(?<!/)[A-Za-z0-9._%+-]+@"
    r"(?!example\.(?:com|net|org|test)\b|users\.noreply\.github\.com)"
    r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)
# Separator is "/", a single "\", or a doubled "\\" (JSON/Python-escaped
# literal) — Path.as_posix(), JSON-serialized tracebacks, and Git Bash all
# produce forward-slash Windows paths, so all three forms need coverage.
# Placeholders (`<username>`, `%USERNAME%`, `...`) are excluded.
_WINPATH_RE = re.compile(r"[A-Za-z]:(?:/|\\{1,2})Users(?:/|\\{1,2})(?!<|%|\.\.\.)[A-Za-z0-9._-]+")
_CRED_RE = re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})\b")

_GENERIC_PII_PATTERNS = (_EMAIL_RE, _WINPATH_RE, _CRED_RE)

# Confirmed-synthetic literals used by tests that exercise a secret-REDACTION
# utility (`scrub_secrets`, CLI-key stripping) or an adversarial-path-scoping
# guard (`is_readonly_allowed`) — the test asserts the value is rejected or
# redacted; it must look real-shaped to prove the guard's own regex actually
# fires. Verified individually as fake, not a pattern weakening.
# Built via concatenation (not a bare literal) so this file itself never
# contains an unbroken secret/path-shaped run — the same discipline this
# guard enforces on the rest of the tree.
_KNOWN_SYNTHETIC_FIXTURE_VALUES = frozenset(
    {
        "sk-" + "abc123DEF456ghi789JKL",  # tests/lean_agent_os/test_terminal_secret_scrub.py
        "ghp_" + "A1b2C3d4E5f6G7h8I9j0",  # tests/lean_agent_os/test_terminal_secret_scrub.py
        "xoxb-" + "1234567890-abcdefghijklmnop",  # tests/lean_agent_os/test_terminal_secret_scrub.py
        "sk-" + "should-be-stripped",  # tests/fix_pass_tars_cockpit_CV_1/test_lane_cli_subscription_env.py
        "C:/Users/" + "attacker",  # tests/lean_agent_os/test_readonly_bash_autoposture.py — adversarial fixture username
        # tesseract/mirror/src-tauri/src/provision.rs `scrub_credentials_*`/
        # `clone_error_message_*` tests — a fake token embedded in a sample
        # clone-error string, to prove the Rust-side credential scrubber
        # actually redacts it. The email-shaped regex fires on the
        # `<token>@github.com` userinfo run; the exact literal only, not the
        # `ghp_` prefix, is allowlisted so any other credential still fails.
        "ghp_supersecret" + "@github.com",
    }
)


def test_production_tree_has_no_generic_pii_or_secrets(tmp_path: Path) -> None:
    """Runs on every machine — no operator token list required."""
    out = tmp_path / "tree"
    build(ROOT, out)
    offenders = []
    for path in out.rglob("*"):
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw:
            # Binary asset (icons, images, fonts, ...) — decoding it as text
            # with errors="ignore" produces noise that coincidentally matches
            # these patterns; this scanner only targets shipped source/text.
            continue
        body = raw.decode("utf-8", errors="ignore")
        for pattern in _GENERIC_PII_PATTERNS:
            for hit in pattern.findall(body):
                value = hit if isinstance(hit, str) else hit[0]
                if value in _KNOWN_SYNTHETIC_FIXTURE_VALUES:
                    continue
                offenders.append(f"{path.relative_to(out)}: {value}")
    assert not offenders, "generic PII/secret pattern in production tree:\n" + "\n".join(offenders)


def test_pii_token_file_itself_never_ships(tmp_path: Path) -> None:
    """`.pii-tokens.local.json` is gitignored/untracked — the allowlist build
    excludes it by construction. Assert it anyway: this is the one file that
    would leak the operator denylist itself if tracking status ever slipped.
    """
    out = tmp_path / "tree"
    build(ROOT, out)
    assert not (out / ".pii-tokens.local.json").exists()
