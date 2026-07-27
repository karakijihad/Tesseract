"""Shared fixtures for AU-5 AutonomyKernel tests.

Every test runs under a monkeypatched ``TESSERACT_HOME`` so the kernel
+ store + worker lane write into ``tmp_path`` — production state stays
untouched per the AU GOVERNANCE §7 rule. Worker lane fixtures use
permissive caps; tests that exercise the lane-full rejection scenario
override the cap explicitly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from tesseract.orchestrator.autonomy import (
    AgendaStore,
    AutonomyEventBus,
    AutonomyKernel,
    KernelConfig,
    MapperConfig,
)
from tesseract.orchestrator.autonomy.models import AgendaSource, RiskClass
from tesseract.orchestrator.autonomy.publishers import set_active_bus
from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.lane import WorkerLane


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def all_mappers_enabled() -> dict[AgendaSource, MapperConfig]:
    """Default mapper config map with every supported source enabled
    so kernel tests don't have to flip them on individually."""
    return {
        source: MapperConfig(
            enabled=True,
            source=source,
            default_risk_class=RiskClass.PROPOSE,
            dedupe_window_hours=24,
        )
        for source in (
            AgendaSource.OPERATOR,
            AgendaSource.PROVIDER_WATCH,
            AgendaSource.SELF_REFLECTION,
            AgendaSource.VAULT_SIGNAL,
        )
    }


@pytest.fixture
def permissive_lane() -> WorkerLane:
    """All five worker kinds with cap=10. Tests that exercise lane-full
    rejection override via ``WorkerLane({kind: 0})``."""
    return WorkerLane(
        {
            WorkerKind.TARS_SELF: 10,
            WorkerKind.MARKDOWN_AGENT: 10,
            WorkerKind.CLAUDE_CLI: 10,
            WorkerKind.CODEX_CLI: 10,
            WorkerKind.TERMINAL: 10,
        }
    )


@pytest.fixture
def kernel(
    isolated_home: Path,
    permissive_lane: WorkerLane,
    all_mappers_enabled: dict[AgendaSource, MapperConfig],
) -> Iterator[AutonomyKernel]:
    store = AgendaStore()
    k = AutonomyKernel(
        agenda_store=store,
        worker_lane=permissive_lane,
        config=KernelConfig(top_k=3, max_concurrent_workers_total=8),
        mapper_configs=all_mappers_enabled,
    )
    yield k
    set_active_bus(None)


@pytest.fixture
def store(isolated_home: Path) -> AgendaStore:
    return AgendaStore()


__all__ = [
    "all_mappers_enabled",
    "isolated_home",
    "kernel",
    "permissive_lane",
    "store",
]
