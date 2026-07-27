"""GET/POST /api/settings/session-caps — tool_iteration_cap and
consecutive_error_cap controls.

Mirrors the compact-threshold pattern: yaml write + in-memory sync +
live-session propagation. DENY rules are *not* exposed and the response
must always carry ``deny_rules_locked: true`` so the frontend can render
that as a read-only row.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.mirror.server.config import ServerConfig, TerminalServerConfig, UploadConfig
from tesseract.mirror.server.routes import settings as settings_route
from tesseract.permissions.policy import PermissionPolicy


def _roles_payload() -> dict:
    return {
        "embeddings": {"primary": "local.ollama.nomic_embed"},
        "roles": {
            "chat_brain": {
                "mode": "active",
                "primary": "api.openai.gpt54_nano",
                "fallbacks": [],
                "compact_threshold": 0.40,
                "keep_recent_turns": 10,
                "tool_iteration_cap": 25,
                "consecutive_error_cap": 3,
                "daily_budget_usd": 3.00,
            },
        },
    }


def _write_roles(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    target = config_dir / "roles.yaml"
    target.write_text(yaml.safe_dump(_roles_payload()), encoding="utf-8")
    return target


def _build_config() -> ServerConfig:
    terminal = TerminalServerConfig(
        default_shell="bash",
        max_tabs=3,
        max_panes_per_tab=3,
        shell_profiles={},
        coalesce_flush_ms=8.0,
        coalesce_flush_chars=4096,
        reattach_grace_s=30.0,
        pause_buffer_cap_chars=2_000_000,
    )
    policy = PermissionPolicy(
        tools_defaults={},
        modes={},
        path_overrides={},
        current_mode="max",
    )
    return ServerConfig(
        host="127.0.0.1",
        port=0,
        entity_name="TARS",
        operator_name="Op",
        cors_origins=(),
        models={
            "roles": {
                "chat_brain": {
                    "tool_iteration_cap": 25,
                    "consecutive_error_cap": 3,
                    "compact_threshold": 0.40,
                    "keep_recent_turns": 10,
                },
            },
        },
        permissions=policy,
        terminal=terminal,
        uploads=UploadConfig(
            max_file_mb=50,
            max_total_mb=50,
            max_files_per_message=5,
            allowed_mime_types=(),
        ),
    )


async def _make_client(tmp_path: Path) -> TestClient:
    _write_roles(tmp_path)
    app = web.Application()
    app["config"] = _build_config()
    app["tesseract_dir"] = tmp_path
    app["server_sessions"] = {}
    app.router.add_get("/api/settings/session-caps", settings_route.get_session_caps)
    app.router.add_post("/api/settings/session-caps", settings_route.set_session_caps)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


async def test_get_session_caps_returns_current_values(tmp_path: Path) -> None:
    client = await _make_client(tmp_path)
    try:
        resp = await client.get("/api/settings/session-caps")
        assert resp.status == 200
        body = await resp.json()
        assert body["tool_iteration_cap"] == 25
        assert body["consecutive_error_cap"] == 3
        assert body["deny_rules_locked"] is True
    finally:
        await client.close()


async def test_set_session_caps_updates_yaml_and_live_session(tmp_path: Path) -> None:
    client = await _make_client(tmp_path)
    app = client.app
    live_cs = SimpleNamespace(max_tool_iterations=25, max_consecutive_adapter_errors=3)
    app["server_sessions"]["s1"] = SimpleNamespace(session_id="s1", chat_session=live_cs)
    try:
        resp = await client.post(
            "/api/settings/session-caps",
            json={"tool_iteration_cap": 40, "consecutive_error_cap": 5},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["tool_iteration_cap"] == 40
        assert body["consecutive_error_cap"] == 5
        assert body["deny_rules_locked"] is True

        # Live session updated.
        assert live_cs.max_tool_iterations == 40
        assert live_cs.max_consecutive_adapter_errors == 5

        # In-memory config updated.
        chat_brain = app["config"].models["roles"]["chat_brain"]
        assert chat_brain["tool_iteration_cap"] == 40
        assert chat_brain["consecutive_error_cap"] == 5

        # roles.yaml on disk updated.
        raw = yaml.safe_load(
            (tmp_path / "config" / "roles.yaml").read_text(encoding="utf-8")
        )
        disk = raw["roles"]["chat_brain"]
        assert disk["tool_iteration_cap"] == 40
        assert disk["consecutive_error_cap"] == 5
    finally:
        await client.close()


async def test_set_session_caps_partial_update_tool_only(tmp_path: Path) -> None:
    client = await _make_client(tmp_path)
    app = client.app
    live_cs = SimpleNamespace(max_tool_iterations=25, max_consecutive_adapter_errors=3)
    app["server_sessions"]["s1"] = SimpleNamespace(session_id="s1", chat_session=live_cs)
    try:
        resp = await client.post(
            "/api/settings/session-caps",
            json={"tool_iteration_cap": 50},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["tool_iteration_cap"] == 50
        assert body["consecutive_error_cap"] == 3
        assert live_cs.max_tool_iterations == 50
        assert live_cs.max_consecutive_adapter_errors == 3
    finally:
        await client.close()


@pytest.mark.parametrize(
    "body, expected_status",
    [
        ({}, 400),  # neither field
        ({"tool_iteration_cap": 0}, 400),  # below min
        ({"tool_iteration_cap": 999}, 400),  # above max
        ({"consecutive_error_cap": 0}, 400),
        ({"consecutive_error_cap": 99}, 400),
        ({"tool_iteration_cap": "abc"}, 400),
        ({"consecutive_error_cap": "abc"}, 400),
    ],
)
async def test_set_session_caps_rejects_bad_inputs(
    tmp_path: Path, body: dict, expected_status: int
) -> None:
    client = await _make_client(tmp_path)
    try:
        resp = await client.post("/api/settings/session-caps", json=body)
        assert resp.status == expected_status
    finally:
        await client.close()


async def test_set_session_caps_invalid_json_returns_400(tmp_path: Path) -> None:
    client = await _make_client(tmp_path)
    try:
        resp = await client.post(
            "/api/settings/session-caps",
            data="not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
    finally:
        await client.close()
