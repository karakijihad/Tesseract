"""Path resolver + session-id minting."""

from __future__ import annotations

import re
from pathlib import Path

from tesseract.orchestrator.tars_controller import paths as pathmod


def test_resolvers_follow_tesseract_home(isolated_home: Path) -> None:
    assert pathmod.controller_dir() == isolated_home / "tars_controller"
    assert pathmod.sessions_dir() == isolated_home / "tars_controller" / "sessions"
    assert pathmod.transcripts_dir() == isolated_home / "tars_controller" / "transcripts"
    assert pathmod.chats_dir() == isolated_home / "sessions" / "chats"
    assert pathmod.controller_record_path() == isolated_home / "tars_controller" / "controller.json"


def test_mint_session_id_shape() -> None:
    sid = pathmod.mint_session_id()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}-[0-9a-f]{8}", sid)


def test_session_record_path_validates(isolated_home: Path) -> None:
    sid = "2026-05-23-deadbeef"
    expected = isolated_home / "tars_controller" / "sessions" / f"{sid}.json"
    assert pathmod.session_record_path(sid) == expected


def test_session_record_path_rejects_traversal(isolated_home: Path) -> None:
    import pytest

    for bad in ("..", "../escape", "2026-05-23-XYZ12345", "evil/../slash", ""):
        with pytest.raises(ValueError):
            pathmod.session_record_path(bad)


def test_transcript_path_uses_jsonl(isolated_home: Path) -> None:
    sid = pathmod.mint_session_id()
    p = pathmod.transcript_path(sid)
    assert p.name == f"{sid}.jsonl"
    assert p.parent == isolated_home / "tars_controller" / "transcripts"
