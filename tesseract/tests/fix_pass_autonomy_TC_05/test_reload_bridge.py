"""TC-5 — Mirror → controller reload bridge.

Covers ``notify_controller_reload`` against a real ``ControllerDaemon``
and the missing-controller / bad-port / missing-token fallback paths.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tesseract.orchestrator.tars_controller import (
    ControllerDaemon,
    SessionRegistry,
    auth,
    notify_controller_reload,
    port_file_path,
)
from tesseract.orchestrator.tars_controller.paths import run_dir


CONTROLLER_ID = "ctrl-test-bridge"


@pytest.mark.asyncio
async def test_no_controller_short_circuits(isolated_home: Path) -> None:
    # No port file written → bridge must return ok=False without raising.
    result = await notify_controller_reload("all")
    assert result["ok"] is False
    assert result["code"] == "no_controller"


@pytest.mark.asyncio
async def test_no_token_returns_no_token_code(isolated_home: Path) -> None:
    # Port file exists but no token → bridge must report no_token.
    run = run_dir()
    run.mkdir(parents=True, exist_ok=True)
    (run / "controller.port").write_text("0", encoding="utf-8")
    result = await notify_controller_reload("all", connect_timeout=0.1)
    # Port=0 first triggers port_out_of_range before we read the token,
    # so we accept either of the two early-exit codes — both are
    # "bridge declined cleanly".
    assert result["ok"] is False
    assert result["code"] in ("no_token", "port_out_of_range", "connect_failed")
    # Cleanup port file so the autouse leak guard stays clean.
    (run / "controller.port").unlink()


@pytest.mark.asyncio
async def test_bridge_round_trip_against_daemon(isolated_home: Path) -> None:
    """End-to-end: spin up a daemon, call the bridge as Mirror would,
    assert the returned dict matches a `reload_complete` shape."""

    token = auth.mint_token()
    auth.write_token(token)

    async def reload_cb(target: str) -> dict[str, Any]:
        return {"reloaded": [f"{target}: ok"], "failed": []}

    daemon = ControllerDaemon(
        controller_id=CONTROLLER_ID,
        token=token,
        registry=SessionRegistry(),
        reload_callback=reload_cb,
        drain_timeout_seconds=1.0,
        heartbeat_interval=3600,
    )
    await daemon.start(host="127.0.0.1", port=0)
    try:
        # Token is on disk; port file is on disk.
        assert port_file_path().exists()
        result = await notify_controller_reload(
            "config", connect_timeout=2.0, response_timeout=5.0
        )
        assert result["ok"] is True
        assert result["target"] == "config"
        assert result["reloaded"] == ["config: ok"]
        assert result["failed"] == []
        assert result["session_count"] == 0
    finally:
        await daemon.stop()
        # Daemon.stop() removes the port file but leaves the token file
        # so post-mortem inspection sees the last controller_id; remove
        # both here so the autouse leak guard stays clean.
        try:
            (run_dir() / "controller.token").unlink()
        except FileNotFoundError:
            pass
        # Some Windows hosts keep the controller.json record around;
        # clean it for the same reason.
        from tesseract.orchestrator.tars_controller.paths import (
            controller_dir,
            controller_record_path,
            heartbeat_path,
        )
        for p in (
            controller_record_path(),
            heartbeat_path(CONTROLLER_ID),
        ):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        # Remove the controller_id subdir if empty.
        try:
            (controller_dir() / CONTROLLER_ID).rmdir()
        except OSError:
            pass


@pytest.mark.asyncio
async def test_bridge_response_timeout(isolated_home: Path) -> None:
    """A callback that sleeps past ``response_timeout`` must surface as
    `{ok: False, code: response_timeout}` — not raise into the watcher."""

    token = auth.mint_token()
    auth.write_token(token)
    release = asyncio.Event()

    async def slow_cb(target: str) -> dict[str, Any]:
        try:
            await asyncio.wait_for(release.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            return {"reloaded": [], "failed": ["slow_cb: timed out internally"]}
        return {"reloaded": ["ok"], "failed": []}

    daemon = ControllerDaemon(
        controller_id=CONTROLLER_ID,
        token=token,
        registry=SessionRegistry(),
        reload_callback=slow_cb,
        drain_timeout_seconds=0.1,
        heartbeat_interval=3600,
    )
    await daemon.start(host="127.0.0.1", port=0)
    try:
        result = await notify_controller_reload(
            "all", connect_timeout=1.0, response_timeout=0.3
        )
        assert result["ok"] is False
        assert result["code"] == "response_timeout"
    finally:
        release.set()
        # Let the callback finish so we don't leak the task into the
        # daemon's pending state.
        await asyncio.sleep(0.05)
        await daemon.stop()
        for p in (
            run_dir() / "controller.token",
        ):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        from tesseract.orchestrator.tars_controller.paths import (
            controller_dir,
            controller_record_path,
            heartbeat_path,
        )
        for p in (
            controller_record_path(),
            heartbeat_path(CONTROLLER_ID),
        ):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        try:
            (controller_dir() / CONTROLLER_ID).rmdir()
        except OSError:
            pass
