import importlib


def _reload_boot():
    import tesseract.paths as paths
    import tesseract.brain.boot as boot
    importlib.reload(paths)
    importlib.reload(boot)
    return boot


def test_env_path_follows_tesseract_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    boot = _reload_boot()
    assert boot.ENV_PATH == (tmp_path / ".env").resolve()


def test_env_path_defaults_to_source_dir_in_dev(monkeypatch):
    monkeypatch.delenv("TESSERACT_HOME", raising=False)
    boot = _reload_boot()
    from tesseract.paths import TESSERACT_DIR
    assert boot.ENV_PATH == (TESSERACT_DIR / ".env")
