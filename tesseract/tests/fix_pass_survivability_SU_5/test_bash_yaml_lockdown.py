"""SU-5: bash absolute-DENY for the four posture YAMLs (check #25).

All assertions against bash_security.check() — pure function, no fixtures needed.
"""

import pytest

from tesseract.permissions import bash_security


def _denied_by_25(cmd: str) -> bool:
    return bash_security.check(cmd) == (25, "blocked")


# ---------------------------------------------------------------------------
# DENY cases — check #25 must fire
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "echo content > tesseract/config/permissions.yaml",
    "echo content >> tesseract/config/roles.yaml",
    "cat foo > tesseract/config/providers.yaml",
    "sed -i 's/x/y/' tesseract/config/mirror.yaml",
    "tee tesseract/config/permissions.yaml < input.txt",
    "Set-Content tesseract/config/providers.yaml -Value 'x'",
    "Out-File tesseract/config/mirror.yaml",
    "Add-Content tesseract/config/roles.yaml -Value 'x'",
    "python -c \"open('tesseract/config/permissions.yaml','w').write('x')\"",
    "echo x > tesseract/Config/Permissions.yaml",           # case-insensitive path
    r"echo x > tesseract\config\permissions.yaml",           # backslash separator
    "echo x>tesseract/config/permissions.yaml",              # no spaces around >
    "cp foo.yaml tesseract/config/permissions.yaml",
    "mv foo.yaml tesseract/config/roles.yaml",
])
def test_check_25_deny(cmd: str) -> None:
    assert _denied_by_25(cmd), f"Expected (25, 'blocked') for: {cmd!r}"


# ---------------------------------------------------------------------------
# ALLOW cases — check #25 must NOT fire
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "cat tesseract/config/permissions.yaml",
    "grep something tesseract/config/roles.yaml",
    "bash -c 'ls tesseract/config/'",
    "head tesseract/config/schedule.yaml",
    "echo x > tesseract/config/schedule.yaml",
])
def test_check_25_allow(cmd: str) -> None:
    assert not _denied_by_25(cmd), f"Expected no (25, 'blocked') for: {cmd!r}"


def test_python_open_read_is_false_positive_acceptable() -> None:
    """Defensive over-trigger: any 'open(' near a locked path denies.

    Per phase-SU-5 §2.1: prefer false-positive on read over miss on write.
    Operator can use file_read tool instead of bash for read access.
    """
    cmd = "python -c \"print(open('tesseract/config/permissions.yaml').read())\""
    assert bash_security.check(cmd) == (25, "blocked")


# ---------------------------------------------------------------------------
# Regression — existing checks unchanged
# ---------------------------------------------------------------------------

def test_rm_rf_still_ask() -> None:
    """check #24 (rm -rf) still returns (24, 'ask') — no regression."""
    assert bash_security.check("rm -rf /tmp/something") == (24, "ask")


# ---------------------------------------------------------------------------
# Structural
# ---------------------------------------------------------------------------

def test_checks_list_length() -> None:
    """_CHECKS list now contains exactly 25 entries."""
    assert len(bash_security._CHECKS) == 25


def test_posture_override_irrelevant() -> None:
    """bash_security.check() is a pure function — posture YAML config has no
    effect on it. Absolute-DENY fires regardless of any policy layer."""
    assert bash_security.check("echo x > tesseract/config/permissions.yaml") == (25, "blocked")


# ---------------------------------------------------------------------------
# False-positive guard — redirect verbs must be positionally adjacent to path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "cmd",
    [
        # `>` used as a comparison operator far from the locked path.
        'python -c "x = 5; print(x > 0)" -- tesseract/config/permissions.yaml',
        # `>` in a string that also mentions the path (verb is > 80 chars before path).
        'echo "tesseract/config/roles.yaml has comparisons like x > 0 inside"',
        # `>` in a comment-like context with the path elsewhere.
        '# x > 0; ls tesseract/config/permissions.yaml',
    ],
    ids=["python_compare", "echo_with_path_and_compare", "comment_with_compare"],
)
def test_redirect_verbs_require_positional_proximity(cmd: str) -> None:
    """`>` / `>>` only count when adjacent to the locked path, not anywhere in cmd."""
    result = bash_security.check(cmd)
    # Either passes (None) OR trips a DIFFERENT check, but NOT #25.
    if result is not None:
        assert result[0] != 25, f"check 25 false-positive on: {cmd}"
