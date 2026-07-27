"""Fixtures for X-4 Session A — lane substrate.

Every test isolates ``TESSERACT_HOME`` so `lanes/` writes land under
``tmp_path``. Per CLAUDE.md hard rule: tests MUST NOT write to the
production tree's runtime state."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def isolated_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Redirect ``TESSERACT_HOME`` to ``tmp_path``. The lane store
    resolves the env var at every call, so no module-attr patching is
    needed.

    P2 Task 2 — real lane spawn (`_HeadlessCliLaneAdapter`) now calls
    `mcp_provision.provision` before the CLI process starts.
    Redirect ``HOME``/``USERPROFILE`` too so a codex-kind lane's
    ``~/.codex/config.toml`` write lands under ``tmp_path``, never the
    operator's real home dir, and pre-set the lane token env vars
    `provision` requires so it doesn't raise mid-test.
    """
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("TESSERACT_MCP_LANE_CLAUDE_TOKEN", "test-lane-claude-token")
    monkeypatch.setenv("TESSERACT_MCP_LANE_CODEX_TOKEN", "test-lane-codex-token")
    return tmp_path


@pytest.fixture(autouse=True)
def _stub_model_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """lane_open validates ``model`` against the providers.yaml cli
    catalog (``tool_support.validate_lane_model``). Stub the catalog so
    tests stay hermetic — never reading the operator's real
    providers.yaml — while reject-path tests pass an id outside this set."""
    from tesseract.orchestrator.tars_controller.lanes import tool_support

    monkeypatch.setattr(
        tool_support,
        "catalog_lane_models",
        lambda kind: frozenset({"claude-sonnet-4-6", "gpt-5-codex"}),
    )
