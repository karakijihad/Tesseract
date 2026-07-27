"""Mirror-edit persistence — Q5 fix. `/schedule-set-cadence` and
`/schedule-enable /schedule-disable` must round-trip through schedule.yaml so
operator changes survive a backend restart. Circuit-breaker / on_failure=disable
mutations must NOT persist (they bypass set_enabled).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tesseract.scheduler.engine import SchedulerEngine, _parse_interval


_BASE_YAML = """\
# operator notes — must survive a round-trip
catchup:
  concurrency: 8
jobs:
  - name: ticker
    cadence: "15m"
    handler: "tesseract.scheduler.tasks.daily_writer.DailyWriterJob"
    enabled: true
    on_failure: log
    retry_policy:
      max_retries: 0
      backoff_seconds: 0
  - name: nightly
    cadence: "0 23 * * *"
    handler: "tesseract.scheduler.tasks.chat_digest.ChatDigestJob"
    enabled: false
    on_failure: log
    retry_policy:
      max_retries: 1
      backoff_seconds: 60
"""


def _engine(tmp_path: Path) -> SchedulerEngine:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "schedule.yaml").write_text(_BASE_YAML, encoding="utf-8")
    return SchedulerEngine(config_dir=cfg_dir)


def _read(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_set_cadence_persists_to_yaml(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine.set_cadence("ticker", "2h30m")

    yaml_path = tmp_path / "config" / "schedule.yaml"
    data = _read(yaml_path)
    assert data["jobs"][0]["cadence"] == "2h30m"
    assert data["jobs"][1]["cadence"] == "0 23 * * *"


def test_set_cadence_preserves_comments(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine.set_cadence("ticker", "30s")

    text = (tmp_path / "config" / "schedule.yaml").read_text(encoding="utf-8")
    assert "# operator notes — must survive a round-trip" in text


def test_set_enabled_persists_to_yaml(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine.set_enabled("ticker", False)
    engine.set_enabled("nightly", True)

    data = _read(tmp_path / "config" / "schedule.yaml")
    assert data["jobs"][0]["enabled"] is False
    assert data["jobs"][1]["enabled"] is True


def test_circuit_break_does_not_persist(tmp_path: Path) -> None:
    """on_failure=disable / circuit-breaker flip rt.enabled directly, not
    through set_enabled. YAML stays authoritative across restarts so a
    transient failure spell doesn't brick a job permanently on disk."""
    engine = _engine(tmp_path)
    engine.registry["ticker"].enabled = False  # simulates breaker trip

    data = _read(tmp_path / "config" / "schedule.yaml")
    assert data["jobs"][0]["enabled"] is True


def test_reload_sees_persisted_cadence(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine.set_cadence("ticker", "45m")

    reloaded = SchedulerEngine(config_dir=tmp_path / "config")
    assert reloaded.registry["ticker"].cfg.cadence == "45m"
    assert reloaded.registry["ticker"].interval_seconds == 45 * 60


def test_set_cadence_rejects_invalid(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with pytest.raises(ValueError):
        engine.set_cadence("ticker", "not a cadence")

    data = _read(tmp_path / "config" / "schedule.yaml")
    assert data["jobs"][0]["cadence"] == "15m"


def test_set_cadence_unknown_job_raises(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with pytest.raises(KeyError):
        engine.set_cadence("ghost", "1h")


def test_parse_interval_compound() -> None:
    assert _parse_interval("2h30m") == 2 * 3600 + 30 * 60
    assert _parse_interval("1d12h") == 86400 + 12 * 3600
    assert _parse_interval("1d") == 86400
    assert _parse_interval("15m") == 15 * 60
    assert _parse_interval("30s") == 30
    assert _parse_interval("0 0 * * *") is None
    assert _parse_interval("") is None
    assert _parse_interval("0m") is None
