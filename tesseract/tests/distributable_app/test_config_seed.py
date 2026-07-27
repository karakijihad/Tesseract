"""Tests for first-run config seeding into a relocated ``TESSERACT_HOME``.

`config_seed.ensure_config_seeded` copies the packaged default config tree
into a fresh, unseeded ``CONFIG_DIR`` (marker: no ``providers.yaml``). It
must never overwrite an already-seeded install, and must be a no-op in dev
where ``CONFIG_DIR`` already equals the source tree (``TESSERACT_DIR/config``).
"""

import importlib


def _reload_config_seed():
    import tesseract.paths as paths
    importlib.reload(paths)
    import tesseract.config_seed as cs
    importlib.reload(cs)
    return cs


def test_seeds_when_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    cs = _reload_config_seed()
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    # empty config dir → seeding populates providers.yaml
    cs.ensure_config_seeded()
    assert (tmp_path / "config" / "providers.yaml").exists()


def test_noop_when_already_seeded(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    cs = _reload_config_seed()
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "providers.yaml").write_text("sentinel: 1\n", encoding="utf-8")
    cs.ensure_config_seeded()
    assert (cfg / "providers.yaml").read_text(encoding="utf-8") == "sentinel: 1\n"  # not overwritten


def test_noop_in_dev(monkeypatch):
    # TESSERACT_HOME unset → CONFIG_DIR == TESSERACT_DIR/config, already
    # populated. ensure_config_seeded must return before attempting any
    # copy-onto-self.
    monkeypatch.delenv("TESSERACT_HOME", raising=False)
    cs = _reload_config_seed()
    from tesseract.paths import TESSERACT_DIR

    real_config = TESSERACT_DIR / "config"
    before = sorted(p.name for p in real_config.glob("*.yaml"))
    cs.ensure_config_seeded()
    after = sorted(p.name for p in real_config.glob("*.yaml"))
    assert before == after
