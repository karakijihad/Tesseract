"""``OperatorNudgeJob`` — periodic "still here, all good" toast.

Built 2026-05-19 after a chat-channel confabulation incident (TARS
claimed "Done. Every 15 minutes I'll fire a toast" with no tool call).
The job's contract is:

* compose a deterministic 1-2 sentence body from local runtime state
  (no LLM call per tick — cheap and auditable),
* route through the AU-10 ``OutboundNotifier`` under a new
  ``operator_nudge`` category (rate-cap aware, mute-respecting),
* never raise — failures surface as ``JobResult(ok=False, ...)``.

Honesty contract: ``failed_workers_recent >= 3`` escalates the band
even when the conscience heartbeat reads ``ok``. The original confab
disease was claim-vs-reality drift; this job must not reproduce it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tesseract.scheduler.tasks.operator_nudge import (
    OperatorNudgeJob,
    _StatusSnapshot,
    _capture_snapshot,
    _compose_body,
    _count_active_agenda,
    _count_paused_sources,
    _count_recent_failed_workers,
    _crash_storm_latched,
    _read_worst_band,
)
from tesseract.scheduler.types import JobContext


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


# -- _compose_body honesty contract ----------------------------------------


def test_compose_body_all_good_when_clean() -> None:
    snap = _StatusSnapshot(
        worst_band="ok",
        active_agenda=3,
        failed_workers_recent=0,
        paused_sources=0,
        crash_storm_latched=False,
    )
    body = _compose_body(snap)
    assert "All good" in body
    assert "3 agenda" in body


def test_compose_body_escalates_on_recent_failures_even_when_conscience_ok() -> None:
    """Regression for the audit-flagged 'looks busy, isn't' pattern.
    Conscience may report OK while workers fail en masse (this is
    precisely the live state codex flagged on 2026-05-19)."""
    snap = _StatusSnapshot(
        worst_band="ok",
        active_agenda=16,
        failed_workers_recent=14,
        paused_sources=0,
        crash_storm_latched=False,
    )
    body = _compose_body(snap)
    assert "All good" not in body
    assert "14 workers failed" in body
    assert "shaky" in body.lower() or "warn" in body.lower() or "bad" in body.lower()


def test_compose_body_crash_storm_overrides_everything() -> None:
    snap = _StatusSnapshot(
        worst_band="ok",
        active_agenda=0,
        failed_workers_recent=0,
        paused_sources=0,
        crash_storm_latched=True,
    )
    body = _compose_body(snap)
    assert "Crash storm latched" in body


def test_compose_body_bad_band_surfaced() -> None:
    snap = _StatusSnapshot(
        worst_band="bad",
        active_agenda=2,
        failed_workers_recent=0,
        paused_sources=1,
        crash_storm_latched=False,
    )
    body = _compose_body(snap)
    assert "BAD" in body


def test_compose_body_unknown_band_says_pending() -> None:
    snap = _StatusSnapshot(
        worst_band="unknown",
        active_agenda=0,
        failed_workers_recent=0,
        paused_sources=0,
        crash_storm_latched=False,
    )
    body = _compose_body(snap)
    assert "Heartbeat pending" in body


# -- snapshot readers behave on cold home ----------------------------------


def test_snapshot_readers_are_safe_on_cold_home(isolated_home: Path) -> None:
    assert _count_active_agenda() == 0
    assert _count_paused_sources() == 0
    assert _count_recent_failed_workers() == 0
    assert _crash_storm_latched() is False
    assert _read_worst_band(isolated_home / "logs" / "conscience") == "unknown"


def test_read_worst_band_picks_latest_line(isolated_home: Path) -> None:
    conscience = isolated_home / "logs" / "conscience"
    conscience.mkdir(parents=True)
    target = conscience / "drift-2026-05-19.jsonl"
    target.write_text(
        "\n".join([
            json.dumps({"summary": {"ok": 5, "warn": 0, "bad": 0}}),
            json.dumps({"summary": {"ok": 4, "warn": 1, "bad": 0}}),
            json.dumps({"summary": {"ok": 3, "warn": 1, "bad": 1}}),
        ]) + "\n",
        encoding="utf-8",
    )
    assert _read_worst_band(conscience) == "bad"


def test_crash_storm_latched_detected(isolated_home: Path) -> None:
    runtime = isolated_home / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "crash_storm.json").write_text("{}", encoding="utf-8")
    assert _crash_storm_latched() is True


# -- JobResult contract ----------------------------------------------------


async def test_job_returns_clean_skip_when_no_notifier(isolated_home: Path) -> None:
    job = OperatorNudgeJob()
    ctx = JobContext(job_name="operator_nudge", app=None)
    result = await job.run(ctx)
    assert result.ok is True
    assert result.payload.get("skipped") is True
    assert result.payload.get("reason") == "no_notifier"


class _StubNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def notify(self, category: str, context: dict) -> "_StubResult":
        self.calls.append((category, context))
        return _StubResult(category=category, sent=1, skipped=False, reason="", errors=0)


class _StubResult:
    def __init__(self, *, category: str, sent: int, skipped: bool, reason: str, errors: int) -> None:
        self.category = category
        self.sent = sent
        self.skipped = skipped
        self.reason = reason
        self.errors = errors


class _DictApp(dict):
    """Mirror app exposes ``.get(key)``; a plain dict matches."""


async def test_job_ships_through_notifier_when_present(isolated_home: Path) -> None:
    notifier = _StubNotifier()
    app = _DictApp()
    app["outbound_notifier"] = notifier
    ctx = JobContext(job_name="operator_nudge", app=app)
    job = OperatorNudgeJob()

    result = await job.run(ctx)

    assert result.ok is True
    assert notifier.calls, "notifier.notify should have been called"
    category, context = notifier.calls[0]
    assert category == "operator_nudge"
    assert "text" in context
    assert isinstance(context["text"], str) and len(context["text"]) > 0
    assert result.payload["body"] == context["text"]
    assert result.payload["sent"] == 1


async def test_job_handles_notifier_exception(isolated_home: Path) -> None:
    class _RaisingNotifier:
        async def notify(self, category: str, context: dict):  # noqa: ANN201
            raise RuntimeError("simulated notify failure")

    app = _DictApp()
    app["outbound_notifier"] = _RaisingNotifier()
    ctx = JobContext(job_name="operator_nudge", app=app)
    job = OperatorNudgeJob()

    result = await job.run(ctx)

    assert result.ok is False
    assert "notify_raised" in result.detail
    assert result.payload.get("snapshot") is not None
