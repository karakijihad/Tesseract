"""Phase 2 — MCP client config loader (`config/mcp_client.py`).

Covers the happy path against the shipped `mcp_servers.yaml` plus every
raise-loudly invariant: required keys, positive timeouts, valid transport,
transport-specific requirements, unique tool_prefix, and the no-inline-secrets
guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tesseract.config.mcp_client import (
    MCP_SERVERS_YAML,
    load_mcp_client_config,
)


def _write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "mcp_servers.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


_VALID_DEFAULTS = {"connect_timeout_s": 15, "tool_call_timeout_s": 60}


# ── happy path: the shipped config ──────────────────────────────────────

def test_ships_valid_and_echo_test_disabled() -> None:
    cfg = load_mcp_client_config(MCP_SERVERS_YAML)
    assert cfg.defaults.connect_timeout_s > 0
    assert cfg.defaults.tool_call_timeout_s > 0
    names = {s.name for s in cfg.servers}
    assert "echo_test" in names
    echo = next(s for s in cfg.servers if s.name == "echo_test")
    assert echo.enabled is False
    assert echo.transport == "stdio"
    assert echo.tool_prefix == "mcp_echo_"
    assert echo.command  # non-empty
    # No real server is enabled by default (curation-first).
    assert cfg.enabled_servers() == ()


def test_empty_servers_is_valid(tmp_path: Path) -> None:
    p = _write(tmp_path, {"defaults": _VALID_DEFAULTS, "servers": {}})
    cfg = load_mcp_client_config(p)
    assert cfg.servers == ()
    assert cfg.enabled_servers() == ()


def test_http_server_with_auth_env_parses(tmp_path: Path) -> None:
    p = _write(tmp_path, {
        "defaults": _VALID_DEFAULTS,
        "servers": {
            "remote": {
                "transport": "http",
                "url": "https://mcp.example.com/mcp",
                "enabled": True,
                "tool_prefix": "mcp_remote_",
                "auth_token_env": "REMOTE_MCP_TOKEN",
            }
        },
    })
    cfg = load_mcp_client_config(p)
    s = cfg.servers[0]
    assert s.transport == "http"
    assert s.url == "https://mcp.example.com/mcp"
    assert s.auth_token_env == "REMOTE_MCP_TOKEN"
    assert cfg.enabled_servers() == (s,)


# ── raise-loudly invariants ─────────────────────────────────────────────

def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_mcp_client_config(tmp_path / "nope.yaml")


def test_missing_defaults_raises(tmp_path: Path) -> None:
    p = _write(tmp_path, {"servers": {}})
    with pytest.raises(RuntimeError, match="defaults"):
        load_mcp_client_config(p)


def test_missing_timeout_key_raises(tmp_path: Path) -> None:
    p = _write(tmp_path, {"defaults": {"connect_timeout_s": 15}, "servers": {}})
    with pytest.raises(RuntimeError, match="tool_call_timeout_s"):
        load_mcp_client_config(p)


def test_nonpositive_timeout_raises(tmp_path: Path) -> None:
    p = _write(tmp_path, {
        "defaults": {"connect_timeout_s": 0, "tool_call_timeout_s": 60},
        "servers": {},
    })
    with pytest.raises(RuntimeError, match="connect_timeout_s"):
        load_mcp_client_config(p)


def test_missing_servers_key_raises(tmp_path: Path) -> None:
    p = _write(tmp_path, {"defaults": _VALID_DEFAULTS})
    with pytest.raises(RuntimeError, match="servers"):
        load_mcp_client_config(p)


def test_duplicate_tool_prefix_raises(tmp_path: Path) -> None:
    p = _write(tmp_path, {
        "defaults": _VALID_DEFAULTS,
        "servers": {
            "a": {"transport": "stdio", "command": ["x"], "enabled": False, "tool_prefix": "dup_"},
            "b": {"transport": "stdio", "command": ["y"], "enabled": False, "tool_prefix": "dup_"},
        },
    })
    with pytest.raises(RuntimeError, match="unique|collides"):
        load_mcp_client_config(p)


def test_inline_secret_raises(tmp_path: Path) -> None:
    p = _write(tmp_path, {
        "defaults": _VALID_DEFAULTS,
        "servers": {
            "leaky": {
                "transport": "http", "url": "https://x/mcp", "enabled": True,
                "tool_prefix": "mcp_x_", "api_key": "sk-secret-inline",
            }
        },
    })
    with pytest.raises(RuntimeError, match="secret"):
        load_mcp_client_config(p)


def test_invalid_transport_raises(tmp_path: Path) -> None:
    p = _write(tmp_path, {
        "defaults": _VALID_DEFAULTS,
        "servers": {"a": {"transport": "carrier-pigeon", "enabled": False, "tool_prefix": "p_"}},
    })
    with pytest.raises(RuntimeError, match="transport"):
        load_mcp_client_config(p)


def test_stdio_without_command_raises(tmp_path: Path) -> None:
    p = _write(tmp_path, {
        "defaults": _VALID_DEFAULTS,
        "servers": {"a": {"transport": "stdio", "enabled": False, "tool_prefix": "p_"}},
    })
    with pytest.raises(RuntimeError, match="command"):
        load_mcp_client_config(p)


def test_http_without_url_raises(tmp_path: Path) -> None:
    p = _write(tmp_path, {
        "defaults": _VALID_DEFAULTS,
        "servers": {"a": {"transport": "http", "enabled": False, "tool_prefix": "p_"}},
    })
    with pytest.raises(RuntimeError, match="url"):
        load_mcp_client_config(p)
