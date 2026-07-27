"""X-4 Session A — ControllerRuntime builds + exposes a live LaneManager.

Mirrors the X-3 provider-registration tests: the runtime owns one
LaneManager instance; the bound-method getter returns the same object;
pre-rebuild the getter returns ``None`` so dependent tools degrade
cleanly instead of crashing."""

from __future__ import annotations

from pathlib import Path

from tesseract.orchestrator.tars_controller.lanes import LaneManager
from tesseract.scripts.tars_controller import ControllerRuntime


def test_rebuild_lane_manager_produces_real_instance(
    isolated_home: Path,
) -> None:
    runtime = ControllerRuntime()
    reloaded, failed = runtime._rebuild_lane_manager()

    assert "lane_manager" in reloaded, failed
    assert failed == []
    assert isinstance(runtime.lane_manager, LaneManager)
    assert runtime._get_lane_manager() is runtime.lane_manager


def test_get_lane_manager_returns_none_before_rebuild() -> None:
    runtime = ControllerRuntime()

    assert runtime.lane_manager is None
    assert runtime._get_lane_manager() is None
