"""Trust store — first-run prompt for the tars CLI.

The trust file lives at ``<TESSERACT_HOME>/trusted_dirs.json``. These
tests use ``isolated_home`` so writes land in tmp_path; the autouse
leak guard catches any escape to the real tree.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tesseract.orchestrator.tars_controller.trust import (
    is_trusted,
    mark_trusted,
    prompt_trust,
    revoke,
)


def test_unseen_cwd_is_not_trusted(isolated_home: Path) -> None:
    assert is_trusted(isolated_home) is False


def test_mark_then_is_trusted(isolated_home: Path) -> None:
    mark_trusted(isolated_home)
    assert is_trusted(isolated_home) is True
    # Persistence: re-read goes through the on-disk JSON.
    payload = json.loads(
        (isolated_home / "trusted_dirs.json").read_text(encoding="utf-8")
    )
    assert str(isolated_home.resolve()) in payload


def test_mark_is_idempotent(isolated_home: Path) -> None:
    mark_trusted(isolated_home)
    mark_trusted(isolated_home)
    mark_trusted(isolated_home)
    payload = json.loads(
        (isolated_home / "trusted_dirs.json").read_text(encoding="utf-8")
    )
    keys = list(payload.keys())
    # Same key with the SAME absolute path collapses to one entry.
    assert keys == [str(isolated_home.resolve())]


def test_revoke_removes_entry(isolated_home: Path) -> None:
    mark_trusted(isolated_home)
    revoke(isolated_home)
    assert is_trusted(isolated_home) is False


def test_prompt_trust_yes_persists(isolated_home: Path) -> None:
    approved = prompt_trust(isolated_home, prompt_fn=lambda _msg: "y")
    assert approved is True
    assert is_trusted(isolated_home) is True


def test_prompt_trust_no_does_not_persist(isolated_home: Path) -> None:
    approved = prompt_trust(isolated_home, prompt_fn=lambda _msg: "n")
    assert approved is False
    assert is_trusted(isolated_home) is False


def test_prompt_trust_blank_treated_as_no(isolated_home: Path) -> None:
    approved = prompt_trust(isolated_home, prompt_fn=lambda _msg: "")
    assert approved is False
    assert is_trusted(isolated_home) is False


def test_prompt_trust_accepts_yes_variants(isolated_home: Path) -> None:
    for answer in ("y", "Y", "yes", "YES", "Yes"):
        revoke(isolated_home)
        assert (
            prompt_trust(isolated_home, prompt_fn=lambda _msg, a=answer: a)
            is True
        )
        assert is_trusted(isolated_home) is True


def test_prompt_trust_eof_treated_as_no(isolated_home: Path) -> None:
    def _eof(_msg: str) -> str:
        raise EOFError("no stdin")

    approved = prompt_trust(isolated_home, prompt_fn=_eof)
    assert approved is False
    assert is_trusted(isolated_home) is False


def test_corrupt_trust_file_is_treated_as_empty(isolated_home: Path) -> None:
    (isolated_home / "trusted_dirs.json").write_text(
        "not json {", encoding="utf-8"
    )
    assert is_trusted(isolated_home) is False
    # And mark_trusted still works (overwrites the bad file).
    mark_trusted(isolated_home)
    assert is_trusted(isolated_home) is True
