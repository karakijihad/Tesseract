"""Phase 18.5 W7-A — `TESSERACT_HOME` env var must redirect user-state
roots without source surgery. Phase 17 bootstrap relies on this so
~/.tesseract can host memory-store/, agents/, vault/, logs/, sessions/
on a packaged install while source code stays at the package install.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


def test_default_home_equals_package_dir() -> None:
    """Without the env var set, TESSERACT_HOME must equal TESSERACT_DIR
    so existing checkouts behave unchanged.

    ``tesseract.paths.TESSERACT_HOME`` is a module-level constant
    initialised at import time. Earlier tests in the suite that
    ``monkeypatch.setenv("TESSERACT_HOME", tmp)`` and trigger an import
    leave the module with a stale tmp value even after pytest cleans the
    env var. Reload here so the assertion compares against the current
    process env, not the first-import snapshot.
    """
    if os.environ.get("TESSERACT_HOME"):
        pytest.skip("TESSERACT_HOME already set in environment")
    import tesseract.paths as paths_mod
    importlib.reload(paths_mod)
    assert paths_mod.TESSERACT_HOME == paths_mod.TESSERACT_DIR


def test_env_var_relocates_home(tmp_path: Path, monkeypatch) -> None:
    """Setting TESSERACT_HOME redirects the runtime user-state root."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    import tesseract.paths as paths_mod
    importlib.reload(paths_mod)
    assert paths_mod.TESSERACT_HOME == tmp_path.resolve()
    # TESSERACT_DIR must not move — source code follows the package.
    assert paths_mod.TESSERACT_DIR != tmp_path.resolve()
    # Restore default for the rest of the suite.
    monkeypatch.delenv("TESSERACT_HOME")
    importlib.reload(paths_mod)


def test_state_dirs_anchor_to_home(tmp_path: Path, monkeypatch) -> None:
    """memory-store / agents / vault / logs / sessions all live under
    TESSERACT_HOME after relocation."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    import tesseract.paths as paths_mod
    import tesseract.brain.boot as boot_mod
    importlib.reload(paths_mod)
    importlib.reload(boot_mod)

    assert boot_mod.SESSIONS_DIR == tmp_path.resolve() / "sessions"
    # The other state roots are constructed at function-call time from
    # TESSERACT_HOME inside boot.build_*; verify that constant points
    # to the relocated root.
    assert boot_mod.TESSERACT_HOME == tmp_path.resolve()

    monkeypatch.delenv("TESSERACT_HOME")
    importlib.reload(paths_mod)
    importlib.reload(boot_mod)


def test_config_paths_stay_anchored_to_package(tmp_path: Path, monkeypatch) -> None:
    """models.yaml / permissions.yaml stay at TESSERACT_DIR even when
    TESSERACT_HOME is set — shipped YAML config travels with the
    package, not with operator state. .env holds user secrets, not
    shipped config, so it follows TESSERACT_HOME (writable per-user
    root) instead."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    import tesseract.paths as paths_mod
    import tesseract.brain.boot as boot_mod
    importlib.reload(paths_mod)
    importlib.reload(boot_mod)

    assert boot_mod.PROVIDERS_YAML.parent.parent == paths_mod.TESSERACT_DIR
    assert boot_mod.ROLES_YAML.parent.parent == paths_mod.TESSERACT_DIR
    assert boot_mod.PERMISSIONS_YAML.parent.parent == paths_mod.TESSERACT_DIR
    assert boot_mod.ENV_PATH.parent == paths_mod.TESSERACT_HOME

    monkeypatch.delenv("TESSERACT_HOME")
    importlib.reload(paths_mod)
    importlib.reload(boot_mod)
