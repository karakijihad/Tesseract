"""mcp-control-plane P4 — the real MCP wire protocol (Streamable-HTTP + JSON-RPC).

Exercises the ``/mcp`` endpoint end-to-end over HTTP exactly as a Claude Code /
Codex CLI client would: ``initialize`` → ``notifications/initialized`` →
``tools/list`` → ``tools/call``, plus session binding, error mapping, the
ASK-pending translation, GET-405, and DELETE session teardown. This is the
in-process proof of the loop ``client → MCP → dispatcher → permission gate →
ActivityRecord`` with no permission bypass.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.config.mcp import load_mcp_config
from tesseract.kernel.tools.base import PermissionResult, Tool, ToolResult
from tesseract.mirror.server.mcp import MCPServer
from tesseract.orchestrator.activity import get_activity_registry
from tesseract.orchestrator.activity.models import ActivityRecord
from tesseract.orchestrator.activity.registry import reset_activity_registry
from tesseract.orchestrator.background_event_bus import reset_background_bus

_TOKEN = "s3cr3t-operator-token"
_TOKEN_ENV = "TESSERACT_MCP_SECRET"
_SESSION_HEADER = "Mcp-Session-Id"

_VALID_YAML = """\
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
  activity.watch: "auto"
  memory.search: "auto"
  memory.save: "ask"
  vault.search: "auto"
  vault.query: "auto"
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


class _CtxCapturingTool(Tool):
    default_posture = "auto"
    risk_class = "autonomous"

    def __init__(self, name: str) -> None:
        self._name = name
        self.captured = None

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
        self.captured = context
        return ToolResult(output="OK")


class _AllowPolicy:
    def get_posture(self, name, tool_input):  # noqa: ANN001
        return PermissionResult.PASSTHROUGH


class _DenyPolicy:
    def get_posture(self, name, tool_input):  # noqa: ANN001
        return PermissionResult.DENY


def _write_yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "mcp.yaml"
    p.write_text(body, encoding="utf-8")
    return p


async def _make_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    policy=None,
    tools=None,
    yaml_body: str = _VALID_YAML,
    clock: Callable[[], float] | None = None,
) -> TestClient:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    monkeypatch.setenv(_TOKEN_ENV, _TOKEN)
    reset_activity_registry()
    reset_background_bus()
    config = load_mcp_config(_write_yaml(tmp_path, yaml_body))

    app = web.Application()
    MCPServer.register_routes(app)
    app["mcp_server"] = MCPServer(config, **({} if clock is None else {"clock": clock}))
    app["repo_root"] = tmp_path
    app["config"] = SimpleNamespace(permissions=policy or _AllowPolicy())
    app["tool_registry"] = SimpleNamespace(tools=tools or {})

    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _auth(token: str = _TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _initialize(client: TestClient, token: str = _TOKEN) -> tuple[dict, str]:
    resp = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
        },
        headers=_auth(token),
    )
    assert resp.status == 200
    session_id = resp.headers[_SESSION_HEADER]
    body = await resp.json()
    return body, session_id


def _sess(session_id: str, token: str = _TOKEN) -> dict[str, str]:
    return {**_auth(token), _SESSION_HEADER: session_id}


# ── auth ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_post_rejects_missing_token(tmp_path, monkeypatch) -> None:
    client = await _make_client(tmp_path, monkeypatch)
    try:
        resp = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert resp.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_rejects_bad_token(tmp_path, monkeypatch) -> None:
    client = await _make_client(tmp_path, monkeypatch)
    try:
        resp = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=_auth("wrong")
        )
        assert resp.status == 401
    finally:
        await client.close()


# ── initialize handshake ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_initialize_returns_capabilities_and_session(tmp_path, monkeypatch) -> None:
    client = await _make_client(tmp_path, monkeypatch)
    try:
        body, session_id = await _initialize(client)
    finally:
        result = body["result"]
        assert result["protocolVersion"] == "2025-06-18"
        assert "tools" in result["capabilities"]
        assert result["serverInfo"]["name"] == "tesseract"
        assert session_id.startswith("mcp:operator:")
        # the session is a live mcp_session Activity record ("who's in the chair")
        rec = get_activity_registry().get(session_id)
        assert rec is not None and rec.kind == "mcp_session" and rec.state == "running"
        await client.close()


@pytest.mark.asyncio
async def test_initialize_negotiates_unknown_version_to_latest(tmp_path, monkeypatch) -> None:
    client = await _make_client(tmp_path, monkeypatch)
    try:
        resp = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "1999-01-01"}},
            headers=_auth(),
        )
        body = await resp.json()
        assert body["result"]["protocolVersion"] == "2025-06-18"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_initialized_notification_is_202(tmp_path, monkeypatch) -> None:
    client = await _make_client(tmp_path, monkeypatch)
    try:
        _, session_id = await _initialize(client)
        resp = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=_sess(session_id),
        )
        assert resp.status == 202
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ping_returns_empty_result(tmp_path, monkeypatch) -> None:
    client = await _make_client(tmp_path, monkeypatch)
    try:
        _, session_id = await _initialize(client)
        resp = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 9, "method": "ping"}, headers=_sess(session_id)
        )
        body = await resp.json()
        assert body["result"] == {}
    finally:
        await client.close()


# ── tools/list ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tools_list_advertises_verbs(tmp_path, monkeypatch) -> None:
    client = await _make_client(tmp_path, monkeypatch)
    try:
        _, session_id = await _initialize(client)
        resp = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=_sess(session_id)
        )
        tools = (await resp.json())["result"]["tools"]
    finally:
        await client.close()
    names = {t["name"] for t in tools}
    assert "activity_list" in names
    assert "memory_search" in names
    for t in tools:
        assert "inputSchema" in t and t["inputSchema"]["type"] == "object"


@pytest.mark.asyncio
async def test_tools_list_memory_save_advertises_real_schema(tmp_path, monkeypatch) -> None:
    # P7 live-gate finding: memory.save advertised only a vague
    # `{"additionalProperties": true}` schema — a client had to guess param
    # shapes and got error_400 three times. tools/list must carry the real
    # MemorySaveInput schema (required type/title/content).
    client = await _make_client(tmp_path, monkeypatch)
    try:
        _, session_id = await _initialize(client)
        resp = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=_sess(session_id)
        )
        tools = (await resp.json())["result"]["tools"]
    finally:
        await client.close()
    save = next(t for t in tools if t["name"] == "memory_save")
    schema = save["inputSchema"]
    assert set(schema["required"]) == {"type", "title", "content"}
    assert "type" in schema["properties"]
    assert "title" in schema["properties"]
    assert "content" in schema["properties"]


@pytest.mark.asyncio
async def test_tools_list_hides_denied_verbs(tmp_path, monkeypatch) -> None:
    # A verb dropped from the allowlist resolves to DENY → not shown (a client
    # never sees a tool it cannot call).
    body = _VALID_YAML.replace('  activity.list: "auto"\n', "")
    client = await _make_client(tmp_path, monkeypatch, yaml_body=body)
    try:
        _, session_id = await _initialize(client)
        resp = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=_sess(session_id)
        )
        names = {t["name"] for t in (await resp.json())["result"]["tools"]}
    finally:
        await client.close()
    assert "activity_list" not in names


# ── tools/call ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tools_call_activity_list(tmp_path, monkeypatch) -> None:
    import json as _json

    client = await _make_client(tmp_path, monkeypatch)
    get_activity_registry().register(
        ActivityRecord(activity_id="lane:abc", kind="lane", label="claude",
                       state="running", durability="persistent")
    )
    try:
        _, session_id = await _initialize(client)
        resp = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "activity_list", "arguments": {}}},
            headers=_sess(session_id),
        )
        result = (await resp.json())["result"]
    finally:
        await client.close()
    assert result["isError"] is False
    ids = [r["activity_id"] for r in _json.loads(result["content"][0]["text"])]
    assert "lane:abc" in ids


@pytest.mark.asyncio
async def test_tools_call_memory_search_through_permission_pipeline(tmp_path, monkeypatch) -> None:
    tools = {"memory_search": _FakeTool("memory_search", "MEMORY-HIT")}
    client = await _make_client(tmp_path, monkeypatch, tools=tools)
    try:
        _, session_id = await _initialize(client)
        resp = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                  "params": {"name": "memory_search", "arguments": {"query": "hi"}}},
            headers=_sess(session_id),
        )
        result = (await resp.json())["result"]
    finally:
        await client.close()
    assert result["isError"] is False
    assert result["content"][0]["text"] == "MEMORY-HIT"


@pytest.mark.asyncio
async def test_tools_call_threads_full_session_id_to_tool_context(tmp_path, monkeypatch) -> None:
    # The tool's ToolContext.session_id must be the FULL mcp_session id
    # (mcp:<client>:<hex>), not just the client name — so two concurrent
    # same-client sessions stay distinguishable downstream.
    tool = _CtxCapturingTool("memory_search")
    client = await _make_client(tmp_path, monkeypatch, tools={"memory_search": tool})
    try:
        _, session_id = await _initialize(client)
        await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                  "params": {"name": "memory_search", "arguments": {"query": "hi"}}},
            headers=_sess(session_id),
        )
    finally:
        await client.close()
    assert tool.captured is not None
    assert tool.captured.session_id == session_id
    assert session_id.count(":") == 2  # mcp:<client>:<hex>


@pytest.mark.asyncio
async def test_tools_call_policy_deny_is_iserror(tmp_path, monkeypatch) -> None:
    tools = {"memory_search": _FakeTool("memory_search", "x")}
    client = await _make_client(tmp_path, monkeypatch, policy=_DenyPolicy(), tools=tools)
    try:
        _, session_id = await _initialize(client)
        resp = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                  "params": {"name": "memory_search", "arguments": {"query": "hi"}}},
            headers=_sess(session_id),
        )
        result = (await resp.json())["result"]
    finally:
        await client.close()
    assert result["isError"] is True
    assert "permission" in result["content"][0]["text"].lower() or "403" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_tools_call_ask_verb_returns_pending(tmp_path, monkeypatch) -> None:
    # memory.save is ASK; no verb_ask_fn wired → pending awaiting_operator handle,
    # surfaced as an isError CallToolResult carrying the approval_id.
    tools = {"memory_save": _FakeTool("memory_save", "saved")}
    client = await _make_client(tmp_path, monkeypatch, tools=tools)
    try:
        _, session_id = await _initialize(client)
        resp = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                  "params": {"name": "memory_save", "arguments": {"content": "x", "type": "user"}}},
            headers=_sess(session_id),
        )
        result = (await resp.json())["result"]
    finally:
        await client.close()
    assert result["isError"] is True
    assert "awaiting_operator" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_tools_call_unknown_tool_is_invalid_params(tmp_path, monkeypatch) -> None:
    client = await _make_client(tmp_path, monkeypatch)
    try:
        _, session_id = await _initialize(client)
        resp = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                  "params": {"name": "bogus_tool", "arguments": {}}},
            headers=_sess(session_id),
        )
        body = await resp.json()
    finally:
        await client.close()
    assert body["error"]["code"] == -32602


# ── session binding + method routing ─────────────────────────────────────

@pytest.mark.asyncio
async def test_request_without_session_is_invalid_request(tmp_path, monkeypatch) -> None:
    client = await _make_client(tmp_path, monkeypatch)
    try:
        resp = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=_auth()
        )
        body = await resp.json()
    finally:
        await client.close()
    assert body["error"]["code"] == -32600


@pytest.mark.asyncio
async def test_unknown_method_is_method_not_found(tmp_path, monkeypatch) -> None:
    client = await _make_client(tmp_path, monkeypatch)
    try:
        _, session_id = await _initialize(client)
        resp = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 8, "method": "resources/list"},
            headers=_sess(session_id),
        )
        body = await resp.json()
    finally:
        await client.close()
    assert body["error"]["code"] == -32601


# ── GET (405) + DELETE (session teardown) ────────────────────────────────

@pytest.mark.asyncio
async def test_get_is_405(tmp_path, monkeypatch) -> None:
    client = await _make_client(tmp_path, monkeypatch)
    try:
        resp = await client.get("/mcp", headers=_auth())
        assert resp.status == 405
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stale_session_rejected_then_reinitialize_recovers(tmp_path, monkeypatch) -> None:
    # Reconnect semantics for the stateless transport: after a session ends, a
    # stale Mcp-Session-Id is rejected (-32600 → client re-initializes); a fresh
    # initialize yields a new working session whose tools/list snapshot is live.
    client = await _make_client(tmp_path, monkeypatch)
    get_activity_registry().register(
        ActivityRecord(activity_id="lane:x", kind="lane", label="l",
                       state="running", durability="persistent")
    )
    try:
        _, stale = await _initialize(client)
        assert (await client.delete("/mcp", headers=_sess(stale))).status == 200
        # stale id now unknown → invalid request
        resp = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=_sess(stale)
        )
        assert (await resp.json())["error"]["code"] == -32600
        # re-initialize → fresh session works and sees the live registry
        _, fresh = await _initialize(client)
        resp2 = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": "activity_list", "arguments": {}}},
            headers=_sess(fresh),
        )
        result = (await resp2.json())["result"]
        assert result["isError"] is False
        assert "lane:x" in result["content"][0]["text"]
    finally:
        await client.close()


# ── idle sweep ───────────────────────────────────────────────────────────

class _FakeClock:
    """Injectable monotonic clock for driving the idle sweep deterministically."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.asyncio
async def test_idle_sweep_closes_stale_session(tmp_path, monkeypatch) -> None:
    clock = _FakeClock()
    client = await _make_client(tmp_path, monkeypatch, clock=clock)
    try:
        _, session_id = await _initialize(client)
        server = client.app["mcp_server"]
        clock.advance(601)  # > idle_timeout_s (600) — never touched since open()
        swept = server._sessions.sweep_idle(server._config.server.idle_timeout_s)
        assert session_id in swept
        assert server._sessions.get(session_id) is None
        rec = get_activity_registry().get(session_id)
        assert rec is not None and rec.state == "closed"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_idle_sweep_spares_recently_touched_session(tmp_path, monkeypatch) -> None:
    clock = _FakeClock()
    client = await _make_client(tmp_path, monkeypatch, clock=clock)
    try:
        _, session_id = await _initialize(client)
        server = client.app["mcp_server"]
        clock.advance(500)
        # a live request touches the session via the POST /mcp choke point
        resp = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 9, "method": "ping"}, headers=_sess(session_id)
        )
        assert resp.status == 200
        clock.advance(500)  # 500s since the touch — still under idle_timeout_s (600)
        swept = server._sessions.sweep_idle(server._config.server.idle_timeout_s)
        assert session_id not in swept
        assert server._sessions.get(session_id) is not None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delete_closes_session(tmp_path, monkeypatch) -> None:
    client = await _make_client(tmp_path, monkeypatch)
    try:
        _, session_id = await _initialize(client)
        resp = await client.delete("/mcp", headers=_sess(session_id))
        assert resp.status == 200
        rec = get_activity_registry().get(session_id)
        assert rec is not None and rec.state == "closed"
        # second delete → unknown session
        resp2 = await client.delete("/mcp", headers=_sess(session_id))
        assert resp2.status == 404
    finally:
        await client.close()
