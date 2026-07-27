"""Audit-4 C3 — doctor split into text-runtime vs voice-runtime modes.

``--doctor`` (full mode) preserves legacy behavior. ``--text-only``
demotes the voice-runtime hard list to soft so Mirror can boot without
``ffmpeg`` on PATH. ``--voice-only`` flips the partition.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tesseract.scripts import check_dependencies as cd


def _force_text_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the text-runtime checks pass without depending on the host's
    actual disk/RAM/env layout. Lets the test isolate the partition logic."""
    monkeypatch.setattr(cd, "_text_runtime_checks", lambda snap: [
        cd.DoctorCheck(name="text_stub", ok=True, detail="forced pass"),
    ])


def test_full_mode_fails_when_ffmpeg_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_text_pass(monkeypatch)
    monkeypatch.setattr(cd.shutil, "which", lambda exe: None)
    hard, _ = cd.run_doctor(mode="full")
    failed = [c for c in hard if not c.ok]
    assert any(c.name == "ffmpeg" for c in failed), "ffmpeg should be hard in full mode"


def test_text_only_passes_when_ffmpeg_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_text_pass(monkeypatch)
    monkeypatch.setattr(cd.shutil, "which", lambda exe: None)
    hard, soft = cd.run_doctor(mode="text-only")
    assert all(c.ok for c in hard), f"text-only hard list should not contain ffmpeg, got: {[c.name for c in hard]}"
    assert any(c.name == "ffmpeg" for c in soft), "ffmpeg should appear in soft list under text-only"


def test_voice_only_fails_when_ffmpeg_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_text_pass(monkeypatch)
    monkeypatch.setattr(cd.shutil, "which", lambda exe: None)
    hard, soft = cd.run_doctor(mode="voice-only")
    assert any(c.name == "ffmpeg" and not c.ok for c in hard), "ffmpeg should be hard under voice-only"
    # text checks demoted to soft
    assert any(c.name == "text_stub" for c in soft), "text-runtime checks should land in soft under voice-only"


def test_main_mutually_exclusive_flags() -> None:
    with patch.object(cd.sys, "argv", ["check_dependencies", "--text-only", "--voice-only"]):
        rc = cd.main()
    assert rc == 2, "passing both --text-only and --voice-only must error out"


def test_main_text_only_returns_zero_when_ffmpeg_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_text_pass(monkeypatch)
    monkeypatch.setattr(cd.shutil, "which", lambda exe: None)
    with patch.object(cd.sys, "argv", ["check_dependencies", "--text-only"]):
        rc = cd.main()
    assert rc == 0
