"""Generic (operator-agnostic) PII shape regexes — single source of truth
shared by `tests/distributable_app/test_no_generic_pii_in_production_tree.py`
and `scripts/audit_release_tree.py`. Needs no operator-specific token list;
detects PII *shapes* (email addresses, Windows user-home paths) that apply
on any machine.
"""

from __future__ import annotations

import re

# Real email local-part must start at a genuine token boundary (not mid-run,
# not right after "/" — which is how `icons/foo@2x.png` asset filenames and
# `https://token@host` URL user-info segments false-positive) and excludes
# IANA-reserved test domains (RFC 2606) plus GitHub's noreply pattern.
EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])(?<!/)[A-Za-z0-9._%+-]+@"
    r"(?!example\.(?:com|net|org|test)\b|users\.noreply\.github\.com)"
    r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)
# Separator is "/", a single "\", or a doubled "\\" (JSON/Python-escaped
# literal) — Path.as_posix(), JSON-serialized tracebacks, and Git Bash all
# produce forward-slash Windows paths, so all three forms need coverage.
# Placeholders (`<username>`, `%USERNAME%`, `...`) are excluded.
WINDOWS_USER_PATH_RE = re.compile(
    r"[A-Za-z]:(?:/|\\{1,2})Users(?:/|\\{1,2})(?!<|%|\.\.\.)[A-Za-z0-9._-]+"
)
