"""Fixtures for the autonomy live-acceptance smoke (mcp-control-plane P5,
Workstream B — promotes the deferred five-scenario smoke to a required gate).

Every test writes under a monkeypatched ``TESSERACT_HOME`` (tmp_path) so the
kernel + agenda store + worker records + operator journal never touch
production state — CLAUDE.md zero-tolerance logs rule.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from tesseract.orchestrator.autonomy import (
    AgendaStore,
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
    """Every AgendaSource enabled — the smoke drives self-reflection,
    vault-signal, and operator-view mappers, none of which the AU-5 six-source
    default turns on."""
    return {
        source: MapperConfig(
            enabled=True,
            source=source,
            default_risk_class=RiskClass.PROPOSE,
            dedupe_window_hours=24,
        )
        for source in AgendaSource
    }


@pytest.fixture
def permissive_lane() -> WorkerLane:
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
