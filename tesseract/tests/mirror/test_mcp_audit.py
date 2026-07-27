"""mcp-control-plane P5 Workstream A — MCP verb audit sink.

Every verb call writes one JSON line to ``<TESSERACT_HOME>/logs/audit/mcp.jsonl``
with a ``params_hash`` (never raw params), for allowed / denied / ASK-pending
outcomes. Path resolves via ``TESSERACT_HOME`` at call time (test-leak-safe).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

from tesseract.config.mcp import load_mcp_config
from tesseract.kernel.tools.base import PermissionResult, Tool, ToolResult
from tesseract.mirror.server.mcp.audit import mcp_audit_path
from tesseract.mirror.server.mcp.auth import authenticate
from tesseract.mirror.server.mcp.dispatcher import _hash_params
from tesseract.mirror.server.mcp import MCPServer
from tesseract.orchestrator.activity.registry import reset_activity_registry
from tesseract.orchestrator.background_event_bus import reset_background_bus

_TOKEN = "operator-token"

_YAML = """\
server:
  host: "127.0.0.1"
  port: 8000
  token_secret_env: "TESSERACT_MCP_SECRET"
  max_connections: 20
  ask_hold_timeout_s: 30
  idle_timeout_s: 600
clients:
  - name: "operator"
    token_env: "TESSERACT_MCP_SECRET"
    trust_tier: "operator"
trust_tiers:
  operator: "auto"
  trusted: "auto"
  restricted: "ask"
verbs:
  activity.list: "auto"
  memory.search: "auto"
  memory.save: "ask"
"""


class _FakeTool(Tool):
    default_posture = "auto"
    risk_class = "autonomous"

    def __init__(self, name: str, output: str) -> None:
        self._name = name
        self._output = output

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "fake"

    @property
    def input_schema(self):  # noqa: ANN201
        from pydantic import BaseModel

        return BaseModel

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input, context) -> ToolResult:  # noqa: ANN001
        return ToolResult(output=self._output)


class _AllowPolicy:
    def get_posture(self, name, tool_input):  # noqa: ANN001
        return PermissionResult.PASSTHROUGH


def _build(tmp_path: Path, monkeypatch, *, tools=None):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    monkeypatch.setenv("TESSERACT_MCP_SECRET", _TOKEN)
    reset_activity_registry()
    reset_background_bus()
    (tmp_path / "mcp.yaml").write_text(_YAML, encoding="utf-8")
    config = load_mcp_config(tmp_path / "mcp.yaml")
    app = web.Application()
    app["mcp_server"] = MCPServer(config)
    app["repo_root"] = tmp_path
    app["config"] = SimpleNamespace(permissions=_AllowPolicy())
    app["tool_registry"] = SimpleNamespace(tools=tools or {})
    return app


async def _dispatch(app, verb, params=None):
    server = app["mcp_server"]
    client = authenticate(server._config, f"Bearer {_TOKEN}")
    return await server._dispatcher.dispatch(app, verb, params or {}, client, ask_fn=server._verb_ask_fn)


def _rows(tmp_path: Path):
    path = mcp_audit_path()
    assert path.exists()
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.asyncio
async def test_allowed_verb_row_has_params_hash_not_raw_params(tmp_path, monkeypatch) -> None:
    app = _build(tmp_path, monkeypatch, tools={"memory_search": _FakeTool("memory_search", "hit")})
    params = {"query": "SUPERSECRETPHRASE"}
    await _dispatch(app, "memory.search", params)
    text = mcp_audit_path().read_text(encoding="utf-8")
    # raw arguments never appear; only their hash
    assert "SUPERSECRETPHRASE" not in text
    row = _rows(tmp_path)[-1]
    assert row["decision"] == "ok"
    assert row["params_hash"] == _hash_params(params)
    assert len(row["params_hash"]) == 16
    assert "query" not in json.dumps(row)  # no param KEYS leaked either


@pytest.mark.asyncio
async def test_denied_verb_writes_row(tmp_path, monkeypatch) -> None:
    app = _build(tmp_path, monkeypatch)
    # vault.search is NOT allowlisted in _YAML → resolve_posture DENY
    status, _ = await _dispatch(app, "vault.search", {"query": "x"})
    assert status == 403
    row = _rows(tmp_path)[-1]
    assert row["decision"] == "deny"
    assert row["params_hash"]


@pytest.mark.asyncio
async def test_ask_pending_verb_writes_awaiting_row(tmp_path, monkeypatch) -> None:
    app = _build(tmp_path, monkeypatch)
    # memory.save is ASK; no verb_ask_fn wired → pending awaiting_operator
    status, _ = await _dispatch(app, "memory.save", {"type": "user", "content": "c"})
    assert status == 202
    row = _rows(tmp_path)[-1]
    assert row["decision"].startswith("awaiting_operator")
    assert row["params_hash"]


@pytest.mark.asyncio
async def test_audit_path_resolves_under_tesseract_home(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    assert mcp_audit_path() == tmp_path / "logs" / "audit" / "mcp.jsonl"


def test_hash_params_is_stable_and_order_independent() -> None:
    assert _hash_params({"a": 1, "b": 2}) == _hash_params({"b": 2, "a": 1})
    assert _hash_params({"a": 1}) != _hash_params({"a": 2})
