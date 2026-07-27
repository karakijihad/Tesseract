"""SU-3b corrective sweep — production registry reachability.

This test calls the live ``build_tool_registry`` with an isolated
``TESSERACT_HOME`` and asserts:

1. The registry boots — every tool's ``risk_class`` is valid.
2. ``delegate_codex_exec`` is reachable through ``registry.tools``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.brain.boot import build_tool_registry


@pytest.fixture
def isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # The autouse conftest fixture sets the env var, but boot.py captures
    # ``TESSERACT_HOME`` as a module-level constant at import time — any
    # writer that uses the constant (e.g. ``EventStore(TESSERACT_HOME / "logs")``
    # at boot.py:1419) would otherwise touch ``tesseract/logs/``. Patch
    # both the source constant and the boot-side re-export to redirect
    # those captures into ``tmp_path`` for the test's duration.
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    monkeypatch.setattr("tesseract.paths.TESSERACT_HOME", tmp_path)
    monkeypatch.setattr("tesseract.brain.boot.TESSERACT_HOME", tmp_path)
    registry, _mood, _voice, _bundle, _alarms = build_tool_registry()
    return registry


def test_build_tool_registry_succeeds(isolated_registry) -> None:
    """Every concrete Tool subclass declares a valid taxonomy ``risk_class``.

    Regression for audit-2 C1: prior to the corrective sweep
    ``DelegateCodexExecTool.risk_class = "read_only"`` raised
    ``RuntimeError`` inside ``_wire_tool_defaults`` and aborted boot.
    """
    assert isolated_registry.tools, "registry built no tools"


def test_delegate_codex_exec_is_registered(isolated_registry) -> None:
    """Companion to the C1 fix — the tool that triggered the boot failure
    must still be registered after its ``risk_class`` was corrected."""
    assert "delegate_codex_exec" in isolated_registry.tools
