"""MO-9-14 — InterestsDecayJob applies exponential half-life and prunes noise.

No-op when the profile file is missing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from tesseract.orchestrator.brief.interests import (
    DEFAULT_HALF_LIFE_DAYS,
    InterestsProfile,
    Signal,
    record_signal,
    save_profile,
)
from tesseract.scheduler.tasks.interests_decay import InterestsDecayJob
from tesseract.scheduler.types import JobContext


@pytest.fixture(autouse=True)
def _tesseract_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


def _ctx(
    *,
    name: str = "interests_decay",
    config: dict | None = None,
) -> JobContext:
    return JobContext(
        job_name=name,
        config=config or {},
        fired_at=datetime(2026, 5, 14, 3, 15, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_decay_job_noop_when_profile_missing(tmp_path: Path) -> None:
    job = InterestsDecayJob()
    result = await job.run(_ctx())
    assert result.ok is True
    assert "skipped" in result.detail
    assert result.payload["skipped"] is True
    # File NOT created.
    assert not (tmp_path / "memory-store" / "interests" / "profile.yaml").exists()


@pytest.mark.asyncio
async def test_decay_job_applies_half_life(tmp_path: Path) -> None:
    profile_path = tmp_path / "memory-store" / "interests" / "profile.yaml"
    seed = InterestsProfile(pillars={"tech": {}, "science": {}, "politics": {}})
    seed = record_signal(seed, "tech", "alpha", Signal.INTERESTED)  # +1.0
    save_profile(seed, profile_path)

    job = InterestsDecayJob()
    result = await job.run(_ctx(config={
        "half_life_days": DEFAULT_HALF_LIFE_DAYS,
        "days": DEFAULT_HALF_LIFE_DAYS,  # exactly one half-life
    }))
    assert result.ok is True
    raw = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    # 1.0 → 0.5 after one half-life
    assert raw["pillars"]["tech"]["alpha"] == pytest.approx(0.5, abs=1e-3)
    assert result.payload["kept_topics"] == 1
    assert result.payload["pruned_topics"] == 0


@pytest.mark.asyncio
async def test_decay_job_prunes_faint_signals(tmp_path: Path) -> None:
    profile_path = tmp_path / "memory-store" / "interests" / "profile.yaml"
    # Pre-set a faint weight that should drop below 0.05 after a heavy decay.
    save_profile(
        InterestsProfile(pillars={
            "tech": {"faint": 0.06, "strong": 5.0},
            "science": {},
            "politics": {},
        }),
        profile_path,
    )
    job = InterestsDecayJob()
    # One half-life: 0.06 → 0.03 (below 0.05 floor → pruned);
    #                5.00 → 2.50 (well above floor → kept).
    result = await job.run(_ctx(config={
        "half_life_days": DEFAULT_HALF_LIFE_DAYS,
        "days": DEFAULT_HALF_LIFE_DAYS,
    }))
    assert result.ok is True
    raw = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    assert "faint" not in raw["pillars"]["tech"]
    assert "strong" in raw["pillars"]["tech"]
    assert raw["pillars"]["tech"]["strong"] == pytest.approx(2.5, abs=1e-3)
    assert result.payload["pruned_topics"] == 1
    assert result.payload["kept_topics"] == 1


@pytest.mark.asyncio
async def test_decay_job_handles_crash_returns_not_ok(tmp_path: Path) -> None:
    """If decay raises (e.g. corrupt YAML), the job returns ok=False."""
    profile_path = tmp_path / "memory-store" / "interests" / "profile.yaml"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    # Valid YAML but pre-existing — the decay function itself won't raise
    # on this shape; we instead force a crash by overriding `load_profile`.
    profile_path.write_text("pillars: {}\n", encoding="utf-8")

    import tesseract.scheduler.tasks.interests_decay as job_mod
    original = job_mod.load_profile

    def _boom(_path):
        raise RuntimeError("forced")

    job_mod.load_profile = _boom  # type: ignore[assignment]
    try:
        result = await InterestsDecayJob().run(_ctx())
    finally:
        job_mod.load_profile = original  # type: ignore[assignment]
    assert result.ok is False
    assert "unhandled" in result.detail
