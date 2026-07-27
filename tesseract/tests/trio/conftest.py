"""trio test fixtures.

Per project hard-rule: every test runs with ``TESSERACT_HOME=tmp_path`` so
nothing can leak into the production tree (``tesseract/logs/**`` zero
tolerance), and hub token env vars are faked so ``mcp_provision.provision``
never refuses nor reads real secrets.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_tokens(monkeypatch):
    monkeypatch.setenv("TESSERACT_MCP_LANE_CLAUDE_TOKEN", "t-lane-claude")
    monkeypatch.setenv("TESSERACT_MCP_LANE_CODEX_TOKEN", "t-lane-codex")
    monkeypatch.setenv("TESSERACT_MCP_TERMINAL_TOKEN", "t-terminal")


class FakeClient:
    def __init__(self, name: str, token_env: str) -> None:
        self.name = name
        self.token_env = token_env


class FakeServer:
    host = "127.0.0.1"
    port = 8000


class FakeMCPConfig:
    """Duck-typed stand-in for config.mcp.MCPConfig — provision() only
    touches .clients (name/token_env) and .server (host/port)."""

    server = FakeServer()
    clients = [
        FakeClient("operator", "TESSERACT_MCP_SECRET"),
        FakeClient("lane-claude", "TESSERACT_MCP_LANE_CLAUDE_TOKEN"),
        FakeClient("lane-codex", "TESSERACT_MCP_LANE_CODEX_TOKEN"),
        FakeClient("terminal-manual", "TESSERACT_MCP_TERMINAL_TOKEN"),
    ]


@pytest.fixture
def mcp_cfg():
    return FakeMCPConfig()
