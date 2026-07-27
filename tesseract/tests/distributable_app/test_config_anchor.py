import importlib


def _reload(mod_name):
    import tesseract.paths as paths
    importlib.reload(paths)
    mod = importlib.import_module(mod_name)
    importlib.reload(mod)
    return mod


def test_config_dir_follows_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    import tesseract.paths as paths
    importlib.reload(paths)
    assert paths.CONFIG_DIR == (tmp_path / "config").resolve() or paths.CONFIG_DIR == tmp_path / "config"


def test_loader_yaml_paths_follow_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    loader = _reload("tesseract.config.loader")
    assert str(tmp_path) in str(loader.PROVIDERS_YAML)
    assert str(tmp_path) in str(loader.ROLES_YAML)


def test_loader_yaml_paths_default_to_source_in_dev(monkeypatch):
    monkeypatch.delenv("TESSERACT_HOME", raising=False)
    loader = _reload("tesseract.config.loader")
    from tesseract.paths import TESSERACT_DIR
    assert str(TESSERACT_DIR) in str(loader.PROVIDERS_YAML)


def test_bypass_sites_follow_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    cfg = _reload("tesseract.mirror.server.config")
    assert str(tmp_path) in str(cfg.PERMISSIONS_YAML)
    assert str(tmp_path) in str(cfg.MIRROR_YAML)
    sp = _reload("tesseract.scripts.sync_permissions")
    assert str(tmp_path) in str(sp.PERMISSIONS_YAML)


def test_mirror_app_tesseract_dir_follows_home(tmp_path, monkeypatch):
    """`app["tesseract_dir"]` (mirror/server/app.py) anchors settings.py's
    yaml read/write helpers — must resolve under TESSERACT_HOME, not the
    source checkout, so an installed app writes config where it can."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    app_mod = _reload("tesseract.mirror.server.app")
    assert str(tmp_path) in str(app_mod.TESSERACT_HOME)


def test_settings_yaml_helpers_follow_config_dir(tmp_path):
    """settings.py's config-path helpers key off `app["tesseract_dir"]`.
    Duck-typed dict stands in for `web.Application` (both support __getitem__)."""
    from tesseract.mirror.server.routes import settings

    fake_app = {"tesseract_dir": tmp_path}
    assert settings._permissions_yaml_path(fake_app) == tmp_path / "config" / "permissions.yaml"
    assert settings._providers_yaml_path(fake_app) == tmp_path / "config" / "providers.yaml"
    assert settings._roles_yaml_path(fake_app) == tmp_path / "config" / "roles.yaml"
    assert settings._mirror_yaml_path(fake_app) == tmp_path / "config" / "mirror.yaml"


def test_logsetup_mirror_yaml_follows_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    logsetup = _reload("tesseract.logsetup")
    assert str(tmp_path) in str(logsetup.MIRROR_YAML)


def test_ecosystem_watchlist_default_follows_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    ecosystem = _reload("tesseract.orchestrator.brief.ecosystem")
    assert str(tmp_path) in str(ecosystem._DEFAULT_WATCHLIST_PATH)


def test_ecosystem_watchlist_default_dev_default(monkeypatch):
    monkeypatch.delenv("TESSERACT_HOME", raising=False)
    ecosystem = _reload("tesseract.orchestrator.brief.ecosystem")
    from tesseract.paths import TESSERACT_DIR
    assert str(TESSERACT_DIR) in str(ecosystem._DEFAULT_WATCHLIST_PATH)


def test_vault_raw_watch_reads_vault_yaml_via_config_dir(tmp_path, monkeypatch):
    """`_load_raw_watch_from_yaml` must resolve `vault.yaml` via CONFIG_DIR,
    not by reconstructing a workspace_root/tesseract/config path — that
    reconstruction bypasses TESSERACT_HOME on a relocated install."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    vrw = _reload("tesseract.scheduler.tasks.vault_raw_watch")

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "vault.yaml").write_text(
        "raw_watch:\n  enabled: false\n  mode: ask_all\n", encoding="utf-8"
    )

    assert vrw.CONFIG_DIR == config_dir

    ctx = vrw.JobContext(job_name="vault_raw_watch_test")
    block = vrw._load_raw_watch_from_yaml(ctx)
    assert block == {"enabled": False, "mode": "ask_all"}


def test_vault_raw_watch_dev_default_matches_source_config(monkeypatch):
    """Backward-compat: with TESSERACT_HOME unset, CONFIG_DIR / 'vault.yaml'
    resolves to the same file the pre-fix code read (tesseract/config/vault.yaml)."""
    monkeypatch.delenv("TESSERACT_HOME", raising=False)
    vrw = _reload("tesseract.scheduler.tasks.vault_raw_watch")
    from tesseract.paths import TESSERACT_DIR
    assert vrw.CONFIG_DIR == TESSERACT_DIR / "config"
