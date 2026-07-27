"""Fixtures for X-5 Session A — persistent named lanes.

Mirrors the X-4 fixture shape: every test isolates ``TESSERACT_HOME``
to ``tmp_path`` so the controller/lanes/ and controller/named-lanes/
roots land under the test scratch dir. Per CLAUDE.md hard rule: tests
MUST NOT write to the production runtime state."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def isolated_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Redirect ``TESSERACT_HOME`` to ``tmp_path``. Both
    `LaneManager` (via store.py) and `NamedLaneManager` (via named.py)
    resolve the env at call time, so no module-attr patching is needed."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _stub_model_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """lane_named_ensure validates ``model`` against the providers.yaml
    cli catalog (``tool_support.validate_lane_model``). Stub the catalog
    so tests stay hermetic — never reading the operator's real
    providers.yaml — while reject-path tests pass an id outside this set."""
    from tesseract.orchestrator.tars_controller.lanes import tool_support

    monkeypatch.setattr(
        tool_support,
        "catalog_lane_models",
        lambda kind: frozenset({"claude-sonnet-4-6", "gpt-5-codex"}),
    )
