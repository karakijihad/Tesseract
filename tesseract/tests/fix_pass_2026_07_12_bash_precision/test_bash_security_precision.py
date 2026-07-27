"""Bash-security precision pass — 2026-07-12.

Live incident (session 2026-07-12-1818): five benign commands hard-denied.

1. Check 12 (octal/hex escapes) fired on Windows backslash date paths —
   ``tars-workshop\\2026-07-12`` matches ``\\[0-7]{3}``. Fixed: bare
   escapes now require a decoding context (``printf`` / ``echo -e``);
   ``$'\\...'`` stays an unconditional block.
2. Check 10 (process substitution) fired on a quoted regex literal
   ``'<body[^>]*>(.*)'`` inside a python one-liner. Tier-shifted
   blocked -> ask (operator-attended, never auto-allow).
3. Security-layer denials now append ``BashTool.security_deny_hint`` so
   the model gets a productive next move instead of a bare denial.
"""

from __future__ import annotations

import pytest

from tesseract.kernel.tools.base import PermissionResult, ToolContext
from tesseract.kernel.tools.bash_tool import BashInput, BashTool
from tesseract.permissions import bash_security
from tesseract.permissions.decide import evaluate


def _ctx() -> ToolContext:
    return ToolContext(
        workspace_root=".",
        session_id="test-bash-precision-2026-07-12",
        current_call_id="call-bash-precision-01",
    )


# ---------------------------------------------------------------------------
# Check 12 — escapes need a decoding context.
# ---------------------------------------------------------------------------

# Verbatim (modulo paths) from the incident session — all previously DENY.
_INCIDENT_PATH_COMMANDS = [
    r"mkdir tesseract\tars-workshop\2026-07-12\pearson-spearman-kendall",
    r"copy /Y tesseract\workspace\a.html tesseract\tars-workshop\2026-07-12\dest\a.html",
    r"mkdir tesseract\tars-workshop\2026-07-12\x 2>nul & copy /Y tesseract\workspace\a.html tesseract\tars-workshop\2026-07-12\x\a.html",
]


@pytest.mark.parametrize("cmd", _INCIDENT_PATH_COMMANDS)
def test_check12_windows_date_paths_pass(cmd: str) -> None:
    assert bash_security.check(cmd) is None


def test_check12_hex_like_path_segment_passes() -> None:
    assert bash_security.check(r"dir build\x64\Release") is None


@pytest.mark.parametrize(
    "cmd",
    [
        "printf '\\101\\102\\103'",
        "printf '\\x41\\x42'",
        "echo -e '\\101'",
        "echo -ne '\\x41'",
    ],
)
def test_check12_decoding_context_still_blocked(cmd: str) -> None:
    assert bash_security.check(cmd) == (12, "blocked")


def test_check12_ansi_c_quoting_still_blocked() -> None:
    assert bash_security.check("echo $'\\x41'") == (12, "blocked")


@pytest.mark.parametrize(
    "cmd",
    [
        # Review finding: decode-and-exec via a non-printf decoder piped to
        # a shell must stay blocked (checks 8/13/17 don't cover this shape).
        "python3 -c \"print('\\x63\\x61\\x74 /etc/shadow')\" | sh",
        "perl -e \"print \\\"\\x63\\x61\\x74\\\"\" | bash",
        "awk 'BEGIN{printf \"\\154\\163\"}' | zsh",
    ],
)
def test_check12_escapes_piped_to_interpreter_still_blocked(cmd: str) -> None:
    result = bash_security.check(cmd)
    assert result is not None
    assert result[1] == "blocked"


# ---------------------------------------------------------------------------
# Check 10 — tier-shifted to ASK.
# ---------------------------------------------------------------------------


def test_check10_process_substitution_is_ask() -> None:
    assert bash_security.check("diff <(cat a) <(cat b)") == (10, "ask")


def test_check10_quoted_regex_incident_command_is_ask_not_blocked() -> None:
    # Trimmed from the incident's python one-liner — the `>(` lives inside
    # a quoted regex literal; ASK lets the operator approve it.
    cmd = (
        "python -c \"import re; m=re.search(r'<body[^>]*>(.*)</body>', txt)\""
    )
    assert bash_security.check(cmd) == (10, "ask")


def test_check10_surfaces_as_ask_permission() -> None:
    decision = BashTool().check_permissions(
        BashInput(command="diff <(cat a) <(cat b)"), _ctx()
    )
    assert decision == PermissionResult.ASK


# ---------------------------------------------------------------------------
# Security-deny hint — decide.evaluate appends BashTool.security_deny_hint.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_security_deny_message_includes_hint(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    tool = BashTool()
    validated = BashInput(command="echo `whoami`")  # check 9 — absolute DENY
    ctx = ToolContext(
        workspace_root=str(tmp_path),
        session_id="test-bash-precision-2026-07-12",
        current_call_id="call-bash-precision-02",
    )
    result = await evaluate(tool, validated, {"command": validated.command}, ctx, None, None)
    assert result is not None
    assert result.is_error is True
    assert result.denied_hard is True
    assert "permission denied: bash" in result.output
    assert "file_copy" in result.output  # the hint names the file tools
