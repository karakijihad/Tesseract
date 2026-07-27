"""Fixtures for the X-3 (tars-cockpit) controller-providers phase."""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.permissions.policy import PermissionPolicy


def make_test_policy() -> PermissionPolicy:
    """Bare-minimum policy (no overrides, headless)."""
    return PermissionPolicy(
        tools_defaults={},
        modes={"headless": {}},
        path_overrides={},
        current_mode="headless",
    )


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``TESSERACT_HOME`` to ``tmp_path``."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def isolated_config_dir(tmp_path: Path) -> Path:
    """Seed a minimal ``schedule.yaml`` so ``SchedulerEngine.__post_init__``
    can load without raising. ``add_job_runtime`` writes the first entry."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "schedule.yaml").write_text(
        "catchup:\n  concurrency: 8\njobs: []\n", encoding="utf-8"
    )
    return config_dir
