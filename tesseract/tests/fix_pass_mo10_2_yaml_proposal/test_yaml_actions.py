"""MO-10-2 §2c/§2d/§2e — three new actions + pre-write checks + apply dedup.

Distributable-app Phase 1, Task 5 exit-gate fix: `apply_yaml_change` now
resolves `tesseract/config/*.yaml` targets under `home_dir() / "config"`
(call-time, honors `TESSERACT_HOME`), not `repo_root` — so these fixtures
seed a home dir's `config/` instead of a `<repo>/tesseract/config/` tree.
"""

from __future__ import annotations

import hashlib
import textwrap
from pathlib import Path

import yaml

from tesseract.kernel.workspace_changes import apply_yaml_change


def _seed_home(tmp_path: Path, monkeypatch) -> Path:
    """Point TESSERACT_HOME at a fresh home dir with a providers.yaml
    carrying a single provider block so the targeted edits have something
    to mutate. The Pydantic schema for providers.yaml validates this shape."""
    home = tmp_path / "home"
    cfg = home / "config"
    cfg.mkdir(parents=True)
    monkeypatch.setenv("TESSERACT_HOME", str(home))
    body = textwrap.dedent(
        """\
        availability:
          max_consecutive_failures: 3
        chain:
          transient_retries: 2
          transient_backoff_ms: 250
          cooldown_max_failures: 1
          cooldown_seconds: 60
        cost_tracking:
          enabled: true
          warning_at_pct: 0.75
          log_file: logs/cost-tracking.jsonl
        api:
          anthropic:
            enabled: false
            api_key_env: ANTHROPIC_API_KEY
            adapter: anthropic
            models:
              opus_47:
                model: claude-opus-4-7
                context_window: 1000000
                cost_per_mtok_in: 15.0
                cost_per_mtok_out: 75.0
        cli: {}
        local: {}
        """
    )
    (cfg / "providers.yaml").write_text(body, encoding="utf-8")
    return home


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_insert_under_path_adds_new_model(tmp_path, monkeypatch):
    home = _seed_home(tmp_path, monkeypatch)
    target = "tesseract/config/providers.yaml"
    full = home / "config" / "providers.yaml"
    before_hash = _hash(full)

    result = apply_yaml_change(
        repo_root=home,  # accepted for signature compat, unused for resolution
        target_path=target,
        action="insert_under_path",
        yaml_path="api.anthropic.models.haiku_45",
        content={"model": "claude-haiku-4-5", "context_window": 200000, "cost_per_mtok_in": 1.0, "cost_per_mtok_out": 5.0},
        expected_hash_before=before_hash,
    )
    assert result.ok, result.reason
    assert result.no_op_reason is None
    doc = yaml.safe_load(full.read_text(encoding="utf-8"))
    assert "haiku_45" in doc["api"]["anthropic"]["models"]
    assert doc["api"]["anthropic"]["models"]["haiku_45"]["context_window"] == 200000


def test_update_field_changes_scalar(tmp_path, monkeypatch):
    home = _seed_home(tmp_path, monkeypatch)
    target = "tesseract/config/providers.yaml"
    full = home / "config" / "providers.yaml"
    result = apply_yaml_change(
        repo_root=home,
        target_path=target,
        action="update_field",
        yaml_path="api.anthropic.models.opus_47.cost_per_mtok_in",
        content=12.0,
        expected_hash_before=_hash(full),
    )
    assert result.ok, result.reason
    doc = yaml.safe_load(full.read_text(encoding="utf-8"))
    assert doc["api"]["anthropic"]["models"]["opus_47"]["cost_per_mtok_in"] == 12.0


def test_append_to_list_appends(tmp_path, monkeypatch):
    home = _seed_home(tmp_path, monkeypatch)
    target = "tesseract/config/providers.yaml"
    full = home / "config" / "providers.yaml"
    # Convert an existing block to seed a list field. Use update_field to
    # add an aliases list first, then append to it.
    apply_yaml_change(
        repo_root=home,
        target_path=target,
        action="update_field",
        yaml_path="api.anthropic.models.opus_47.aliases",
        content=["claude-opus-4-7"],
        expected_hash_before=_hash(full),
    )
    result = apply_yaml_change(
        repo_root=home,
        target_path=target,
        action="append_to_list_at_path",
        yaml_path="api.anthropic.models.opus_47.aliases",
        content="claude-4.7-opus",
        expected_hash_before=_hash(full),
    )
    assert result.ok, result.reason
    doc = yaml.safe_load(full.read_text(encoding="utf-8"))
    assert doc["api"]["anthropic"]["models"]["opus_47"]["aliases"] == ["claude-opus-4-7", "claude-4.7-opus"]


def test_drift_check_refuses_stale_hash(tmp_path, monkeypatch):
    _seed_home(tmp_path, monkeypatch)
    target = "tesseract/config/providers.yaml"
    result = apply_yaml_change(
        repo_root=tmp_path,
        target_path=target,
        action="update_field",
        yaml_path="api.anthropic.models.opus_47.cost_per_mtok_in",
        content=99.0,
        expected_hash_before="0" * 64,
    )
    assert not result.ok
    assert result.reason == "drift_detected"


def test_schema_violation_rejects_unknown_top_key(tmp_path, monkeypatch):
    home = _seed_home(tmp_path, monkeypatch)
    target = "tesseract/config/providers.yaml"
    full = home / "config" / "providers.yaml"
    result = apply_yaml_change(
        repo_root=home,
        target_path=target,
        action="insert_under_path",
        yaml_path="garbage_top_key",
        content={"x": 1},
        expected_hash_before=_hash(full),
    )
    assert not result.ok
    assert "schema_violation" in result.reason


def test_apply_dedup_returns_no_op(tmp_path, monkeypatch):
    home = _seed_home(tmp_path, monkeypatch)
    target = "tesseract/config/providers.yaml"
    full = home / "config" / "providers.yaml"
    result = apply_yaml_change(
        repo_root=home,
        target_path=target,
        action="update_field",
        yaml_path="api.anthropic.models.opus_47.context_window",
        content=1000000,
        expected_hash_before=_hash(full),
    )
    assert result.ok
    assert result.no_op_reason == "duplicate"


def test_target_outside_config_dir_rejected(tmp_path, monkeypatch):
    """A yaml_change_proposal target that isn't under `tesseract/config/`
    must be refused, not silently resolved somewhere unexpected."""
    _seed_home(tmp_path, monkeypatch)
    result = apply_yaml_change(
        repo_root=tmp_path,
        target_path="tesseract/mirror/mirror.yaml",
        action="update_field",
        yaml_path="x",
        content=1,
        expected_hash_before="0" * 64,
    )
    assert not result.ok
    assert "not under" in result.reason
