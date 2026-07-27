"""Controller IPC token round-trip."""

from __future__ import annotations

from pathlib import Path

from tesseract.orchestrator.tars_controller import auth, token_file_path


def test_mint_token_returns_uuid4_string(isolated_home: Path) -> None:
    token = auth.mint_token()
    assert isinstance(token, str)
    assert len(token) == 36
    assert token.count("-") == 4


def test_write_and_read_token_round_trip(isolated_home: Path) -> None:
    token = auth.mint_token()
    path = auth.write_token(token)
    assert path == token_file_path()
    assert path.read_text(encoding="utf-8") == token
    assert auth.read_token() == token


def test_read_token_returns_none_when_missing(isolated_home: Path) -> None:
    assert auth.read_token() is None


def test_verify_token_constant_time(isolated_home: Path) -> None:
    token = auth.mint_token()
    assert auth.verify_token(token, token)
    assert not auth.verify_token("nope", token)
    assert not auth.verify_token("", token)
    assert not auth.verify_token(token, "")
