"""Phase 6 — observer always-on. No per-pane consent prompt.

Pins:
- New panes auto-grant observer consent when the global state is armed
  or observing (matches operator directive 2026-05-16 — "remove the
  observer asking if he can see the terminal").
- Spawning while merely armed promotes state to observing.
- New panes spawned while disarmed do NOT auto-grant.
- grant_consent_for_all_live() bulk-grants on arm() so re-arming
  immediately resumes observation without waiting for new panes.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web

from tesseract.mirror.server.config import (
    ShellProfile,
    TerminalServerConfig,
)
from tesseract.mirror.server.pty_manager import PTYEntry, PTYManager


@pytest.fixture(autouse=True)
def _isolate_log_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))


def _make_app(observer_state: str = "armed") -> web.Application:
    app = web.Application()
    app["observer"] = SimpleNamespace(
        observe_incremental=lambda *a, **k: None,
        reset=lambda: None,
        drop_pty_for_pane=lambda *a, **k: None,
    )
    app["observer_state"] = observer_state
    app["observer_consented_panes"] = set()
    return app


def _make_manager_bound(app: web.Application) -> PTYManager:
    cfg = TerminalServerConfig(
        default_shell="cmd",
        max_tabs=4,
        max_panes_per_tab=4,
        shell_profiles={"cmd": ShellProfile(argv=("cmd.exe",), label="cmd")},
        coalesce_flush_ms=8.0,
        coalesce_flush_chars=4096,
        reattach_grace_s=30.0,
        pause_buffer_cap_chars=2_000_000,
    )
    manager = PTYManager(cfg)
    manager.bind_app(app)
    return manager


def _seed_entry(manager: PTYManager, pane_id: str = "pty_a") -> PTYEntry:
    proc = SimpleNamespace(isalive=lambda: True, write=lambda *a, **k: None,
                           terminate=lambda *a, **k: None, read=lambda *a, **k: "")
    ws = SimpleNamespace(closed=False)
    entry = PTYEntry(pane_id=pane_id, shell="cmd", proc=proc, ws=ws)  # type: ignore[arg-type]
    manager._ptys[pane_id] = entry  # noqa: SLF001
    return entry


# ─── _maybe_auto_grant_consent ────────────────────────────────────


def test_auto_grant_no_op_when_observer_off() -> None:
    app = _make_app(observer_state="off")
    manager = _make_manager_bound(app)
    _seed_entry(manager, "pty_off")
    manager._maybe_auto_grant_consent("pty_off")  # noqa: SLF001
    assert "pty_off" not in app["observer_consented_panes"]
    assert app["observer_state"] == "off"


def test_auto_grant_promotes_armed_to_observing() -> None:
    app = _make_app(observer_state="armed")
    manager = _make_manager_bound(app)
    _seed_entry(manager, "pty_arm")
    manager._maybe_auto_grant_consent("pty_arm")  # noqa: SLF001
    assert "pty_arm" in app["observer_consented_panes"]
    assert app["observer_state"] == "observing"


def test_auto_grant_idempotent_when_already_observing() -> None:
    app = _make_app(observer_state="observing")
    manager = _make_manager_bound(app)
    _seed_entry(manager, "pty_obs")
    manager._maybe_auto_grant_consent("pty_obs")  # noqa: SLF001
    assert app["observer_consented_panes"] == {"pty_obs"}
    assert app["observer_state"] == "observing"
    # Calling again is a no-op (set semantics) and doesn't downgrade state.
    manager._maybe_auto_grant_consent("pty_obs")  # noqa: SLF001
    assert app["observer_consented_panes"] == {"pty_obs"}
    assert app["observer_state"] == "observing"


def test_auto_grant_skips_unknown_pane_id() -> None:
    """grant_consent silently drops grants for non-live pane_ids
    (pr-review SEC-4 carry-over) — the auto-grant path inherits this."""
    app = _make_app(observer_state="armed")
    manager = _make_manager_bound(app)
    manager._maybe_auto_grant_consent("not_a_live_pane")  # noqa: SLF001
    assert app["observer_consented_panes"] == set()


# ─── grant_consent_for_all_live ───────────────────────────────────


def test_grant_consent_for_all_live_bulk_grants_new_panes() -> None:
    app = _make_app(observer_state="armed")
    manager = _make_manager_bound(app)
    _seed_entry(manager, "pty_a")
    _seed_entry(manager, "pty_b")
    _seed_entry(manager, "pty_c")

    granted = manager.grant_consent_for_all_live()

    assert granted == 3
    assert app["observer_consented_panes"] == {"pty_a", "pty_b", "pty_c"}


def test_grant_consent_for_all_live_skips_already_consented() -> None:
    app = _make_app(observer_state="armed")
    manager = _make_manager_bound(app)
    _seed_entry(manager, "pty_a")
    _seed_entry(manager, "pty_b")
    app["observer_consented_panes"].add("pty_a")  # pre-existing consent

    granted = manager.grant_consent_for_all_live()

    assert granted == 1
    assert app["observer_consented_panes"] == {"pty_a", "pty_b"}


def test_grant_consent_for_all_live_returns_zero_when_empty() -> None:
    app = _make_app(observer_state="armed")
    manager = _make_manager_bound(app)
    assert manager.grant_consent_for_all_live() == 0
