"""Per-job `model_role` plumbing for LLM-using scheduler jobs.

Covers:

1.  `JobConfig` parses + persists the optional field, ruamel round-trip
    leaves operator comments intact.
2.  Engine validation refuses unknown roles and refuses model_role on
    non-LLM handlers (loud at boot per CLAUDE.md "no silent gates").
3.  `JobContext.model_role` reaches the handler.
4.  `runtime_state` payload exposes `uses_llm`, `model_role`,
    `default_model_role`, `effective_model_role` for the Mirror dropdown.
5.  Shared `build_chain_for_job` / `resolve_role_name` honor the override
    over the default.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
import yaml

from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.engine import SchedulerEngine
from tesseract.scheduler.role_chain import resolve_role_name
from tesseract.scheduler.types import JobContext, JobResult


_FIXTURE_YAML = """\
# operator comment — must survive
catchup:
  concurrency: 8
jobs:
  - name: digest
    cadence: "30 20 * * *"
    handler: "tesseract.scheduler.tasks.chat_digest.ChatDigestJob"
    enabled: true
    on_failure: log
    retry_policy:
      max_retries: 1
      backoff_seconds: 60

  - name: rollup
    cadence: "0 20 * * *"
    handler: "tesseract.scheduler.tasks.daily_writer.DailyWriterJob"
    enabled: true
    on_failure: log
    retry_policy:
      max_retries: 0
      backoff_seconds: 0
"""


def _write_yaml(tmp_path: Path) -> Path:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "schedule.yaml").write_text(_FIXTURE_YAML, encoding="utf-8")
    return cfg_dir


def _read_yaml(cfg_dir: Path) -> dict:
    return yaml.safe_load((cfg_dir / "schedule.yaml").read_text(encoding="utf-8"))


def test_jobconfig_parses_optional_model_role(tmp_path: Path) -> None:
    cfg_dir = _write_yaml(tmp_path)
    engine = SchedulerEngine(config_dir=cfg_dir)
    digest = engine.registry["digest"].cfg
    rollup = engine.registry["rollup"].cfg
    assert digest.model_role is None  # absent in YAML → None
    assert rollup.model_role is None


def test_set_model_role_persists_and_preserves_comments(tmp_path: Path) -> None:
    cfg_dir = _write_yaml(tmp_path)
    engine = SchedulerEngine(config_dir=cfg_dir)

    fake_bundle = mock.Mock()
    fake_bundle.roles = {"chat_brain": object(), "subagents_default": object()}
    # `_validate_model_role` imports `load_bundle` from `tesseract.brain.boot`
    # at call time, so patch it on the source module.
    with mock.patch("tesseract.brain.boot.load_bundle", return_value=fake_bundle):
        engine.set_model_role("digest", "subagents_default")

    data = _read_yaml(cfg_dir)
    assert data["jobs"][0]["model_role"] == "subagents_default"
    text = (cfg_dir / "schedule.yaml").read_text(encoding="utf-8")
    assert "# operator comment — must survive" in text
    assert engine.registry["digest"].cfg.model_role == "subagents_default"


def test_set_model_role_clear_reverts_to_default(tmp_path: Path) -> None:
    cfg_dir = _write_yaml(tmp_path)
    engine = SchedulerEngine(config_dir=cfg_dir)
    fake_bundle = mock.Mock()
    fake_bundle.roles = {"chat_brain": object()}
    with mock.patch("tesseract.brain.boot.load_bundle", return_value=fake_bundle):
        engine.set_model_role("digest", "chat_brain")
        engine.set_model_role("digest", None)
    assert engine.registry["digest"].cfg.model_role is None
    data = _read_yaml(cfg_dir)
    assert data["jobs"][0]["model_role"] is None


def test_set_model_role_rejects_non_llm_handler(tmp_path: Path) -> None:
    cfg_dir = _write_yaml(tmp_path)
    engine = SchedulerEngine(config_dir=cfg_dir)
    with pytest.raises(ValueError):
        # daily_writer (`rollup`) does not set uses_llm — override is meaningless.
        engine.set_model_role("rollup", "chat_brain")


def test_set_model_role_rejects_unknown_role(tmp_path: Path) -> None:
    cfg_dir = _write_yaml(tmp_path)
    engine = SchedulerEngine(config_dir=cfg_dir)
    fake_bundle = mock.Mock()
    fake_bundle.roles = {"chat_brain": object()}  # 'mystery_role' missing
    with mock.patch("tesseract.brain.boot.load_bundle", return_value=fake_bundle):
        with pytest.raises(RuntimeError, match="not defined in roles.yaml"):
            engine.set_model_role("digest", "mystery_role")


def test_runtime_state_exposes_role_fields(tmp_path: Path) -> None:
    cfg_dir = _write_yaml(tmp_path)
    engine = SchedulerEngine(config_dir=cfg_dir)
    snap = engine.runtime_state("digest")
    assert snap["uses_llm"] is True
    assert snap["model_role"] is None
    assert snap["default_model_role"] == "chat_brain"
    assert snap["effective_model_role"] == "chat_brain"

    rollup = engine.runtime_state("rollup")
    assert rollup["uses_llm"] is False
    assert rollup["default_model_role"] is None
    assert rollup["effective_model_role"] is None


def test_runtime_state_reflects_override(tmp_path: Path) -> None:
    cfg_dir = _write_yaml(tmp_path)
    engine = SchedulerEngine(config_dir=cfg_dir)
    fake_bundle = mock.Mock()
    fake_bundle.roles = {"chat_brain": object(), "subagents_default": object()}
    with mock.patch("tesseract.brain.boot.load_bundle", return_value=fake_bundle):
        engine.set_model_role("digest", "subagents_default")
    snap = engine.runtime_state("digest")
    assert snap["model_role"] == "subagents_default"
    assert snap["effective_model_role"] == "subagents_default"
    assert snap["default_model_role"] == "chat_brain"


def test_resolve_role_name_prefers_override() -> None:
    ctx = JobContext(job_name="x", model_role="subagents_default")
    assert resolve_role_name(ctx, "chat_brain") == "subagents_default"


def test_resolve_role_name_falls_back_to_default() -> None:
    ctx = JobContext(job_name="x")
    assert resolve_role_name(ctx, "chat_brain") == "chat_brain"


def test_resolve_role_name_strips_whitespace() -> None:
    ctx = JobContext(job_name="x", model_role="  ")
    assert resolve_role_name(ctx, "chat_brain") == "chat_brain"


def test_jobcontext_model_role_default_none() -> None:
    ctx = JobContext(job_name="x")
    assert ctx.model_role is None


def test_reload_jobs_picks_up_out_of_band_model_role(tmp_path: Path) -> None:
    """Operator edits schedule.yaml directly while the engine is running.
    The config watcher calls `reload_jobs()`, which must (a) diff
    `model_role` like cadence/enabled/etc., (b) re-validate against
    roles.yaml, and (c) update the in-memory cfg so the next fire picks
    it up.
    """
    cfg_dir = _write_yaml(tmp_path)
    engine = SchedulerEngine(config_dir=cfg_dir)
    assert engine.registry["digest"].cfg.model_role is None

    # Out-of-band edit — operator changes schedule.yaml directly.
    yaml_path = cfg_dir / "schedule.yaml"
    text = yaml_path.read_text(encoding="utf-8")
    patched = text.replace(
        '    on_failure: log\n    retry_policy:\n      max_retries: 1',
        '    on_failure: log\n    model_role: subagents_default\n    retry_policy:\n      max_retries: 1',
        1,
    )
    yaml_path.write_text(patched, encoding="utf-8")

    fake_bundle = mock.Mock()
    fake_bundle.roles = {"chat_brain": object(), "subagents_default": object()}
    with mock.patch("tesseract.brain.boot.load_bundle", return_value=fake_bundle):
        diff = engine.reload_jobs()

    assert "digest" in diff["changed"]
    assert engine.registry["digest"].cfg.model_role == "subagents_default"


def test_reload_jobs_raises_on_unknown_model_role(tmp_path: Path) -> None:
    """Out-of-band edit referencing a role missing from roles.yaml must
    halt the reload — silent fallback would mask a typo for weeks."""
    cfg_dir = _write_yaml(tmp_path)
    engine = SchedulerEngine(config_dir=cfg_dir)

    yaml_path = cfg_dir / "schedule.yaml"
    text = yaml_path.read_text(encoding="utf-8")
    patched = text.replace(
        '    on_failure: log\n    retry_policy:\n      max_retries: 1',
        '    on_failure: log\n    model_role: ghost_role\n    retry_policy:\n      max_retries: 1',
        1,
    )
    yaml_path.write_text(patched, encoding="utf-8")

    fake_bundle = mock.Mock()
    fake_bundle.roles = {"chat_brain": object()}  # ghost_role absent
    with mock.patch("tesseract.brain.boot.load_bundle", return_value=fake_bundle):
        with pytest.raises(RuntimeError, match="not defined in roles.yaml"):
            engine.reload_jobs()


def test_basejob_subclass_can_declare_uses_llm() -> None:
    class _Demo(BaseJob):
        uses_llm = True
        default_model_role = "chat_brain"

        async def run(self, ctx: JobContext) -> JobResult:  # pragma: no cover
            return JobResult(job_name=ctx.job_name, run_id=ctx.run_id, ok=True)

    assert _Demo.uses_llm is True
    assert _Demo.default_model_role == "chat_brain"
    assert BaseJob.uses_llm is False
    assert BaseJob.default_model_role is None
