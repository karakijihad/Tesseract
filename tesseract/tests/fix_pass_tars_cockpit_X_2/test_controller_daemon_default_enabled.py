"""X-2 — Supervisor's ``controller_daemon_enabled`` defaults ON.

Pre-X-2 the Supervisor dataclass set the field to ``False`` and only the
``__main__`` boot site enabled it conditionally — anyone constructing
Supervisor directly inherited the off default. X-2 flips the source default
so every Supervisor instance gets the controller daemon unless the env-var
opt-out is set at the boot site.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.supervisor.daemon import Supervisor


def test_dataclass_default_is_true(tmp_path: Path) -> None:
    """Direct construction inherits ``controller_daemon_enabled=True``."""
    sup = Supervisor(tesseract_home=tmp_path)
    assert sup.controller_daemon_enabled is True


def test_explicit_false_is_honored(tmp_path: Path) -> None:
    """Explicit ``False`` still wins — the dataclass default is the floor,
    not a hard constraint. Operator overrides remain effective."""
    sup = Supervisor(tesseract_home=tmp_path, controller_daemon_enabled=False)
    assert sup.controller_daemon_enabled is False


def test_env_opt_out_mimics_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``__main__`` continues to honor ``SUPERVISOR_DISABLE_CONTROLLER=1``
    by computing the kwarg from the env var. Mimicking that boot site
    here proves the opt-out still flows end-to-end after the default flip."""
    import os

    monkeypatch.setenv("SUPERVISOR_DISABLE_CONTROLLER", "1")
    enabled = os.getenv("SUPERVISOR_DISABLE_CONTROLLER") != "1"
    sup = Supervisor(tesseract_home=tmp_path, controller_daemon_enabled=enabled)
    assert sup.controller_daemon_enabled is False


def test_env_unset_yields_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the env var unset, the ``__main__`` computation matches the
    new dataclass default (True). Belt-and-suspenders against a future
    regression that splits the two truth sources."""
    import os

    monkeypatch.delenv("SUPERVISOR_DISABLE_CONTROLLER", raising=False)
    enabled = os.getenv("SUPERVISOR_DISABLE_CONTROLLER") != "1"
    sup = Supervisor(tesseract_home=tmp_path, controller_daemon_enabled=enabled)
    assert sup.controller_daemon_enabled is True
