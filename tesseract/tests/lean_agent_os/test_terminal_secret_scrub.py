"""Phase 5 Task 3 — secret scrub applied at the PTY observer capture point.

`scrub_secrets()` lives alongside `strip_ansi()` in
`orchestrator/terminal/end_of_turn.py` (both are PTY-byte-stream utilities
shared by the observer capture path). Covers common token/key shapes per
the plan (`Docs/Plan/lean-agent-os/phase-5-terminal.md` Task 3): provider-
prefixed API keys, AWS access key ids, Bearer auth headers, key/token/
password assignments, and long high-entropy base64/hex runs.
"""

from __future__ import annotations

from tesseract.orchestrator.terminal.end_of_turn import scrub_secrets


def test_openai_style_key_redacted() -> None:
    out = scrub_secrets("export OPENAI_API_KEY=sk-abc123DEF456ghi789JKL\n")
    assert "sk-abc123DEF456ghi789JKL" not in out
    assert "[redacted]" in out


def test_github_pat_redacted() -> None:
    out = scrub_secrets("git remote set-url origin https://ghp_A1b2C3d4E5f6G7h8I9j0@github.com/x/y\n")
    assert "ghp_A1b2C3d4E5f6G7h8I9j0" not in out
    assert "[redacted]" in out


def test_slack_bot_token_redacted() -> None:
    out = scrub_secrets("SLACK_TOKEN=xoxb-1234567890-abcdefghijklmnop\n")
    assert "xoxb-1234567890-abcdefghijklmnop" not in out
    assert "[redacted]" in out


def test_aws_access_key_id_redacted() -> None:
    out = scrub_secrets("aws_access_key_id = AKIAIOSFODNN7EXAMPLE\n")
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "[redacted]" in out


def test_bearer_token_redacted_scheme_kept() -> None:
    out = scrub_secrets("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVA\n")
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVA" not in out
    assert "Bearer [redacted]" in out


def test_password_assignment_redacted_key_kept() -> None:
    out = scrub_secrets("password: hunter2\n")
    assert "hunter2" not in out
    assert "password:" in out
    assert "[redacted]" in out


def test_token_colon_assignment_redacted() -> None:
    out = scrub_secrets("token: abcXYZ789\n")
    assert "abcXYZ789" not in out
    assert "[redacted]" in out


def test_key_equals_assignment_redacted() -> None:
    out = scrub_secrets("key=deadbeefcafe\n")
    assert "deadbeefcafe" not in out
    assert "[redacted]" in out


def test_long_hex_run_redacted() -> None:
    hex_run = "a1b2c3d4e5f6" * 3  # 36 hex chars, >= 32
    out = scrub_secrets(f"checksum {hex_run} ok\n")
    assert hex_run not in out
    assert "[redacted]" in out


def test_long_base64_run_redacted() -> None:
    b64_run = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0NTY3" # 41 chars base64-ish
    out = scrub_secrets(f"blob: {b64_run}\n")
    assert b64_run not in out


def test_short_hex_not_redacted() -> None:
    out = scrub_secrets("commit abc1234 pushed\n")
    assert out == "commit abc1234 pushed\n"


def test_ordinary_output_unchanged() -> None:
    raw = "$ ls -la\ntotal 24\ndrwxr-xr-x  3 op op 4096 Jul  5 12:00 .\n"
    assert scrub_secrets(raw) == raw


def test_empty_and_none_safe() -> None:
    assert scrub_secrets("") == ""


def test_multiple_secrets_in_one_chunk_all_redacted() -> None:
    raw = "token=sk-abc123DEF456ghi789JKL and password: hunter2\n"
    out = scrub_secrets(raw)
    assert "sk-abc123DEF456ghi789JKL" not in out
    assert "hunter2" not in out
