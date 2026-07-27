"""P7 Task 2 — repo-upgrade research scheduler job + repo_upgrade mapper.

Covers:
- canned codex output → publish_to_bus(REPO_UPGRADE, ...); real mapper turns
  the resulting event into a valid AgendaItem draft
- repeat publish of the same finding (same target, same day, identical
  event_id) → real kernel admission dedupes to one open item
- repeat publish of the same underlying finding with a DIFFERENT event_id
  and slightly reworded text (the production shape: weekly cadence +
  non-deterministic Codex phrasing) → real kernel admission dedupes via
  `find_fuzzy_dedupe`, not the exact source_event_id match
- missing `targets` / missing `timeout_s` config → ok=False, detail names key
- codex timeout on one of two targets → other target still published,
  ok=True with detail noting the failure; both timing out → ok=False
- REPO_UPGRADE ∈ agenda.yaml::vetter.vet_required → item mints UNVETTED via
  real kernel admission
- a promoted (PROPOSED) REPO_UPGRADE item still carries an unfulfilled
  operator_review approval gate → real kernel selection parks it at
  AWAITING_OPERATOR instead of dispatching to a worker
- no logs pollution under tesseract/logs/ from the test run
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from tesseract.orchestrator.autonomy.drafts import AgendaItemDraft
from tesseract.orchestrator.autonomy.event_bus import AutonomyEvent, AutonomyEventBus
from tesseract.orchestrator.autonomy.kernel import (
    REASON_AWAITING_OPERATOR,
    AutonomyKernel,
    KernelConfig,
    MapperConfig,
)
from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.mappers.repo_upgrade import map as map_repo_upgrade
from tesseract.orchestrator.autonomy.models import AgendaItem, AgendaSource, AgendaStatus, RiskClass
from tesseract.orchestrator.autonomy import publishers
from tesseract.orchestrator.workers.lane import WorkerLane
from tesseract.scheduler.tasks import repo_upgrade_research
from tesseract.scheduler.tasks.repo_upgrade_research import RepoUpgradeResearchJob
from tesseract.scheduler.types import JobContext

# Corrected depth vs the known AU_20-precedent bug (see brief): this file is
# tesseract/tests/lean_agent_os_p7/test_repo_upgrade_research.py, so
# parents[2] is `tesseract/`, not the repo root.
_REPO_LOGS = Path(__file__).resolve().parents[2] / "logs"


def _logs_snapshot() -> dict[str, int]:
    """Recursive, per-file-size snapshot — a top-level-name-only diff would
    miss an appended line to an existing file, which is exactly the
    zero-tolerance case the project's logs-pollution rule exists to catch."""
    if not _REPO_LOGS.exists():
        return {}
    return {
        str(p.relative_to(_REPO_LOGS)): p.stat().st_size
        for p in _REPO_LOGS.rglob("*")
        if p.is_file()
    }


@pytest.fixture(autouse=True)
def _guard_logs_pollution():
    """Zero-tolerance guard (CLAUDE.md): no test may write under
    tesseract/logs/. Snapshot at THIS test's setup and re-check at its
    teardown, so the comparison window spans only this test's own
    execution — not the whole collection→run span, which false-positives
    on any concurrent live-service write (mirror-backend.log, tokenjuice/,
    …) during a slow suite."""
    before = _logs_snapshot()
    yield
    after = _logs_snapshot()
    assert after == before, (
        f"tesseract/logs/ changed during this test. before={before} after={after}"
    )


# ── Fakes ───────────────────────────────────────────────────────────


def _patch_runner(monkeypatch: pytest.MonkeyPatch, outcomes: dict[str, str | Exception]) -> None:
    """``outcomes`` keyed by the path substring expected in the prompt."""

    async def _fake_run(prompt: str, timeout_s: float) -> str:
        for path, outcome in outcomes.items():
            if path in prompt:
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        raise AssertionError(f"no fake outcome matched prompt: {prompt[:200]}")

    monkeypatch.setattr(repo_upgrade_research, "_make_codex_runner", lambda: _fake_run)


def _ctx(*, config: dict[str, Any], fired_at: datetime | None = None) -> JobContext:
    return JobContext(
        job_name="repo_upgrade_research",
        fired_at=fired_at or datetime(2026, 7, 6, 6, 0, tzinfo=timezone.utc),
        app={},
        config=config,
    )


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _isolate_bus():
    bus = AutonomyEventBus()
    publishers.set_active_bus(bus)
    yield bus
    publishers.set_active_bus(None)


def _build_kernel(*, config: KernelConfig, bus: AutonomyEventBus) -> AutonomyKernel:
    return AutonomyKernel(
        agenda_store=AgendaStore(),
        worker_lane=WorkerLane.from_mission_lanes_block({}),
        config=config,
        mappers={AgendaSource.REPO_UPGRADE: map_repo_upgrade},
        mapper_configs={
            AgendaSource.REPO_UPGRADE: MapperConfig(
                enabled=True,
                source=AgendaSource.REPO_UPGRADE,
                default_risk_class=RiskClass.PROPOSE,
                dedupe_window_hours=24,
            )
        },
        event_bus=bus,
    )


# ── Test 1: canned output → publish_to_bus → real mapper → real AgendaItem ──


async def test_happy_path_publishes_and_mapper_produces_valid_draft(
    monkeypatch: pytest.MonkeyPatch, _isolate_bus: AutonomyEventBus,
) -> None:
    _patch_runner(monkeypatch, {
        "tesseract/scheduler": "requests==2.28 is outdated, upstream is 2.32; "
        "consider pinning httpx instead.",
    })
    ctx = _ctx(config={
        "targets": [{"path": "tesseract/scheduler", "focus": "dependency freshness"}],
        "timeout_s": 30,
    })

    result = await RepoUpgradeResearchJob().run(ctx)

    assert result.ok is True
    buffered = _isolate_bus.peek(AgendaSource.REPO_UPGRADE)
    assert len(buffered) == 1
    event = buffered[0]
    assert event.payload["path"] == "tesseract/scheduler"
    assert "requests==2.28" in event.payload["findings"]

    drafts = map_repo_upgrade(event)
    assert len(drafts) == 1
    draft = drafts[0]
    assert isinstance(draft, AgendaItemDraft)
    assert draft.source is AgendaSource.REPO_UPGRADE
    assert draft.risk_class is RiskClass.PROPOSE
    assert draft.source_event_id == event.event_id
    assert "tesseract/scheduler" in draft.goal
    assert "dependency freshness" in draft.rationale

    item = draft.to_item()
    assert isinstance(item, AgendaItem)
    assert item.id.startswith("ag-")
    assert item.source is AgendaSource.REPO_UPGRADE
    assert item.goal == draft.goal


# ── Test 2: repeat publish (same target, same day) → kernel dedupes ──


async def test_repeat_publish_same_day_dedupes_to_one_open_item(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, _isolate_bus: AutonomyEventBus,
) -> None:
    _patch_runner(monkeypatch, {
        "tesseract/scheduler": "requests==2.28 is outdated, upstream is 2.32.",
    })
    kernel = _build_kernel(config=KernelConfig(top_k=3), bus=_isolate_bus)
    ctx = _ctx(config={
        "targets": [{"path": "tesseract/scheduler", "focus": "dependency freshness"}],
        "timeout_s": 30,
    })

    first_result = await RepoUpgradeResearchJob().run(ctx)
    assert first_result.ok is True
    first_tick = await kernel.tick()
    assert first_tick.items_created == 1

    # Same target, same fired_at day — a re-run (e.g. next scheduler tick
    # replaying, or an operator-triggered retry) publishes the identical
    # finding again.
    second_result = await RepoUpgradeResearchJob().run(ctx)
    assert second_result.ok is True
    second_tick = await kernel.tick()
    assert second_tick.items_created == 0
    assert second_tick.items_deduped == 1

    items = kernel._agenda.list_active()
    assert len(items) == 1


async def test_repeat_finding_different_event_id_dedupes_via_fuzzy_match(
    isolated_home: Path, _isolate_bus: AutonomyEventBus,
) -> None:
    """Production shape: weekly cadence + non-deterministic Codex phrasing
    means a repeat of the same underlying finding arrives with a DIFFERENT
    event_id and slightly reworded text — the exact source_event_id
    short-circuit in `_persist_draft` does NOT fire here; only the fuzzy
    near-duplicate check (`AgendaStore.find_fuzzy_dedupe`) catches it."""
    kernel = _build_kernel(config=KernelConfig(top_k=3), bus=_isolate_bus)

    first_event = AutonomyEvent.make(
        AgendaSource.REPO_UPGRADE,
        {
            "path": "tesseract/scheduler",
            "focus": "dependency freshness",
            "findings": "requests==2.28 is outdated, upstream is 2.32.",
        },
        event_id="evt_repo_upgrade_run_one",
    )
    kernel.bus.publish_nowait(first_event)
    first_tick = await kernel.tick()
    assert first_tick.items_created == 1

    second_event = AutonomyEvent.make(
        AgendaSource.REPO_UPGRADE,
        {
            "path": "tesseract/scheduler",
            "focus": "dependency freshness",
            "findings": "requests==2.28 is outdated; upstream has released 2.32.",
        },
        event_id="evt_repo_upgrade_run_two",  # different id — same underlying finding
    )
    assert second_event.event_id != first_event.event_id
    kernel.bus.publish_nowait(second_event)
    second_tick = await kernel.tick()
    assert second_tick.items_created == 0
    assert second_tick.items_deduped == 1

    items = kernel._agenda.list_active()
    assert len(items) == 1


# ── Test 3: missing config ──


async def test_missing_targets_fails() -> None:
    ctx = _ctx(config={"timeout_s": 30})
    result = await RepoUpgradeResearchJob().run(ctx)
    assert result.ok is False
    assert "targets" in result.detail


async def test_missing_timeout_s_fails() -> None:
    ctx = _ctx(config={"targets": [{"path": "tesseract/scheduler", "focus": "x"}]})
    result = await RepoUpgradeResearchJob().run(ctx)
    assert result.ok is False
    assert "timeout_s" in result.detail


# ── Test 4: partial + total codex timeout ──


async def test_one_target_times_out_other_still_published(
    monkeypatch: pytest.MonkeyPatch, _isolate_bus: AutonomyEventBus,
) -> None:
    _patch_runner(monkeypatch, {
        "tesseract/good": "good target findings: dependency X is outdated.",
        "tesseract/dead": asyncio.TimeoutError(),
    })
    ctx = _ctx(config={
        "targets": [
            {"path": "tesseract/good", "focus": "x"},
            {"path": "tesseract/dead", "focus": "y"},
        ],
        "timeout_s": 30,
    })

    result = await RepoUpgradeResearchJob().run(ctx)

    assert result.ok is True
    assert "tesseract/dead" in result.detail
    buffered = _isolate_bus.peek(AgendaSource.REPO_UPGRADE)
    assert len(buffered) == 1
    assert buffered[0].payload["path"] == "tesseract/good"


async def test_all_targets_time_out_fails(
    monkeypatch: pytest.MonkeyPatch, _isolate_bus: AutonomyEventBus,
) -> None:
    _patch_runner(monkeypatch, {
        "tesseract/dead1": asyncio.TimeoutError(),
        "tesseract/dead2": asyncio.TimeoutError(),
    })
    ctx = _ctx(config={
        "targets": [
            {"path": "tesseract/dead1", "focus": "x"},
            {"path": "tesseract/dead2", "focus": "y"},
        ],
        "timeout_s": 30,
    })

    result = await RepoUpgradeResearchJob().run(ctx)

    assert result.ok is False
    assert _isolate_bus.peek(AgendaSource.REPO_UPGRADE) == []


# ── Test 5: REPO_UPGRADE ∈ vet_required → mints UNVETTED via real kernel ──


async def test_repo_upgrade_source_mints_unvetted(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, _isolate_bus: AutonomyEventBus,
) -> None:
    _patch_runner(monkeypatch, {
        "tesseract/scheduler": "requests==2.28 is outdated, upstream is 2.32.",
    })
    kernel = _build_kernel(
        config=KernelConfig(
            top_k=3,
            vet_enabled=True,
            vet_required=frozenset({AgendaSource.REPO_UPGRADE}),
        ),
        bus=_isolate_bus,
    )
    ctx = _ctx(config={
        "targets": [{"path": "tesseract/scheduler", "focus": "dependency freshness"}],
        "timeout_s": 30,
    })

    result = await RepoUpgradeResearchJob().run(ctx)
    assert result.ok is True
    tick = await kernel.tick()
    assert tick.items_created == 1

    items = kernel._agenda.list_active()
    assert len(items) == 1
    assert items[0].status == AgendaStatus.UNVETTED


def test_production_agenda_yaml_carries_repo_upgrade_in_vet_required() -> None:
    """Direct check on the production config this task was required to
    edit (tesseract/config/agenda.yaml::vetter.vet_required) — not just an
    inline KernelConfig construction."""
    agenda_yaml = Path(__file__).resolve().parents[2] / "config" / "agenda.yaml"
    raw = yaml.safe_load(agenda_yaml.read_text(encoding="utf-8")) or {}
    vet_required = (raw.get("vetter") or {}).get("vet_required") or []
    assert "repo_upgrade" in vet_required


# ── Approval gate: promoted item still requires operator sign-off ──


def test_mapper_attaches_unfulfilled_operator_review_gate() -> None:
    """"No auto-apply of anything, ever" — every repo_upgrade draft carries
    an unfulfilled operator_review ApprovalGate, mirroring the strategist
    mapper's unconditional-gate pattern (kernel.py's admission gate never
    dispatches a PROPOSED item with an unfulfilled approval)."""
    event = AutonomyEvent.make(
        AgendaSource.REPO_UPGRADE,
        {
            "path": "tesseract/scheduler",
            "focus": "dependency freshness",
            "findings": "requests==2.28 is outdated, upstream is 2.32.",
        },
    )
    draft = map_repo_upgrade(event)[0]
    assert len(draft.approvals_required) == 1
    gate = draft.approvals_required[0]
    assert gate.kind == "operator_review"
    assert gate.target.startswith("repo_upgrade:")
    assert gate.fulfilled is False


async def test_promoted_item_awaits_operator_not_dispatched(
    isolated_home: Path, _isolate_bus: AutonomyEventBus,
) -> None:
    """Simulates the post-vetter state: REPO_UPGRADE mints straight to
    PROPOSED (vetter already promoted it — vet_enabled=False here stands
    in for "already vetted"). Real kernel selection must still park it at
    AWAITING_OPERATOR — never select/dispatch it to a worker — because the
    mapper's operator_review gate is unfulfilled."""
    kernel = _build_kernel(config=KernelConfig(top_k=3, vet_enabled=False), bus=_isolate_bus)
    kernel.bus.publish_nowait(
        AutonomyEvent.make(
            AgendaSource.REPO_UPGRADE,
            {
                "path": "tesseract/scheduler",
                "focus": "dependency freshness",
                "findings": "requests==2.28 is outdated, upstream is 2.32.",
            },
        )
    )

    result = await kernel.tick()

    assert result.items_created == 1
    assert result.selected == []
    assert any(r["reason"] == REASON_AWAITING_OPERATOR for r in result.rejections)
    items = kernel._agenda.list_active()
    assert len(items) == 1
    assert items[0].status == AgendaStatus.AWAITING_OPERATOR


# ── Distillation: obedience-preamble echo must never become the goal ──


async def test_preamble_echo_is_not_distilled_into_goal(
    monkeypatch: pytest.MonkeyPatch, _isolate_bus: AutonomyEventBus,
) -> None:
    """Regression for the live P7 gate finding: a published proposal's
    goal was 'review repo-upgrade findings for tesseract/scheduler:
    Understood. I'll perform read-only inspection only: ...' — the
    codex obedience preamble, not the actual findings."""
    _patch_runner(monkeypatch, {
        "tesseract/scheduler": (
            "Understood. I'll perform read-only inspection only: no file "
            "edits, deletes, or mutating commands.\n"
            "requests==2.28 is outdated, upstream is 2.32; consider "
            "pinning httpx instead."
        ),
    })
    ctx = _ctx(config={
        "targets": [{"path": "tesseract/scheduler", "focus": "dependency freshness"}],
        "timeout_s": 30,
    })

    result = await RepoUpgradeResearchJob().run(ctx)
    assert result.ok is True

    event = _isolate_bus.peek(AgendaSource.REPO_UPGRADE)[0]
    assert "Understood" not in event.payload["summary"]
    assert "I'll" not in event.payload["summary"]
    assert "requests==2.28" in event.payload["summary"]

    draft = map_repo_upgrade(event)[0]
    assert "Understood" not in draft.goal
    assert "I'll" not in draft.goal
    assert "requests==2.28" in draft.goal


async def test_live_gate_pure_preamble_output_falls_back_to_deterministic_title(
    monkeypatch: pytest.MonkeyPatch, _isolate_bus: AutonomyEventBus,
) -> None:
    """Regression for the REOPENED live P7 gate finding (commit 24c57875's
    fix was insufficient): the live run's actual Codex output was a SINGLE
    line consisting entirely of an obedience preamble, with no substantive
    findings line following it at all —

        "Understood. I'll perform only read-only inspection and report
        findings, with no writes, edits, deletes, generated files,
        environment changes, or git operations."

    The old ``_distill_summary`` skipped this line in its scan loop (it
    matched the preamble regex) but then fell through to
    ``return findings.strip()[:_MAX_SUMMARY_CHARS]`` — which re-echoed the
    exact same preamble text it had just rejected. The goal ended up:
    "review repo-upgrade findings for tesseract/scheduler: Understood.
    I'll perform only read-only inspection and report findings, with no
    writes, edits, deletes, generated files, environment changes, or g..."
    """
    live_preamble = (
        "Understood. I'll perform only read-only inspection and report "
        "findings, with no writes, edits, deletes, generated files, "
        "environment changes, or git operations."
    )
    _patch_runner(monkeypatch, {"tesseract/scheduler": live_preamble})
    ctx = _ctx(config={
        "targets": [{"path": "tesseract/scheduler", "focus": "dependency freshness"}],
        "timeout_s": 30,
    })

    result = await RepoUpgradeResearchJob().run(ctx)
    assert result.ok is True

    event = _isolate_bus.peek(AgendaSource.REPO_UPGRADE)[0]
    assert "Understood" not in event.payload["summary"]
    assert "I'll" not in event.payload["summary"]
    assert "tesseract/scheduler" in event.payload["summary"]

    draft = map_repo_upgrade(event)[0]
    assert "Understood" not in draft.goal
    assert "I'll" not in draft.goal


async def test_proposal_marker_is_lifted_exactly(
    monkeypatch: pytest.MonkeyPatch, _isolate_bus: AutonomyEventBus,
) -> None:
    _patch_runner(monkeypatch, {
        "tesseract/scheduler": (
            "Understood. I'll stay read-only.\n"
            "PROPOSAL: pin httpx to replace requests==2.28\n"
            "\n"
            "Details: requests==2.28 is outdated, upstream is 2.32."
        ),
    })
    ctx = _ctx(config={
        "targets": [{"path": "tesseract/scheduler", "focus": "dependency freshness"}],
        "timeout_s": 30,
    })

    result = await RepoUpgradeResearchJob().run(ctx)
    assert result.ok is True

    event = _isolate_bus.peek(AgendaSource.REPO_UPGRADE)[0]
    assert event.payload["summary"] == "pin httpx to replace requests==2.28"

    draft = map_repo_upgrade(event)[0]
    assert "pin httpx to replace requests==2.28" in draft.goal


