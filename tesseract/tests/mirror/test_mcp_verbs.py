"""mcp-control-plane P3 session 1 — MCPVerbDispatcher + write verbs.

Covers the ASK-over-MCP contract (approve/decline/awaiting-handle), DENY → 403,
trust-tier cap tightening, and the per-call audit sink — for the kernel-tool-
backed write verbs (memory.save/update, vault.ingest).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.config.mcp import load_mcp_config
from tesseract.kernel.tools.base import PermissionResult, Tool, ToolResult
from tesseract.mirror.server.mcp import MCPServer
from tesseract.mirror.server.mcp.approvals import (
    MCPApprovalRegistry,
    build_verb_ask_fn,
)
from tesseract.mirror.server.mcp.audit import mcp_audit_path
from tesseract.mirror.server.routes import mcp_approvals as mcp_approvals_route
from tesseract.orchestrator.activity import get_activity_registry
from tesseract.orchestrator.activity.models import ActivityRecord
from tesseract.orchestrator.activity.registry import reset_activity_registry
from tesseract.orchestrator.background_event_bus import reset_background_bus

_TOKEN = "operator-token"
_RESTRICTED_TOKEN = "restricted-token"

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
  - name: "bot"
    token_env: "TESSERACT_MCP_RESTRICTED"
    trust_tier: "restricted"
trust_tiers:
  operator: "auto"
  trusted: "auto"
  restricted: "ask"
verbs:
  activity.list: "auto"
  activity.watch: "auto"
  activity.cancel: "ask"
  memory.search: "auto"
  memory.save: "ask"
  memory.update: "ask"
  vault.ingest: "ask"
  lane.read: "auto"
  schedule.run: "ask"
  surface.spawn: "ask"
  surface.focus: "auto"
  surface.close: "ask"
  budget.status: "auto"
  budget.set_cap: "ask"
  budget.pause_source: "ask"
  agent.assign: "ask"
  agent.status: "auto"
  agent.review: "auto"
"""


class _FakeTool(Tool):
    default_posture = "auto"
    risk_class = "autonomous"

    def __init__(self, name: str, output: str) -> None:
        self._name = name
        self._output = output
        self.ran = False

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
        return False

    async def run(self, tool_input, context) -> ToolResult:  # noqa: ANN001
        self.ran = True
        return ToolResult(output=self._output)


class _CtxCapturingTool(Tool):
    """AUTO tool that records the ToolContext it was handed — used to assert the
    MCP path wires substrate providers off the app."""

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


class _FakeSpawns:
    """Stand-in for a session's SpawnRegistry — activity.cancel(delegate) cancels
    the matching handle across app['server_sessions']."""

    def __init__(self, ids) -> None:
        self._ids = set(ids)
        self.cancelled: list[str] = []

    async def cancel(self, handle_id) -> bool:  # noqa: ANN001
        if handle_id in self._ids:
            self.cancelled.append(handle_id)
            return True
        return False


def _register(record: ActivityRecord) -> None:
    get_activity_registry().register(record)


class _FakeLedger:
    """Minimal stand-in for CostLedger — the budget verbs read/control it."""

    def __init__(self) -> None:
        self.caps: dict[str, float] = {}
        self.paused: set[str] = set()

    def budget_summary(self):  # noqa: ANN201
        return {
            "enabled": True,
            "spent_usd": 1.0,
            "cap_usd": 10.0,
            "per_role_caps": dict(self.caps),
            "paused_sources": sorted(self.paused),
        }

    def budget_state(self, role):  # noqa: ANN001,ANN201
        return SimpleNamespace(role_spent_usd=0.5, role_cap_usd=5.0, blocked=False)

    def set_role_cap(self, role, cap):  # noqa: ANN001
        self.caps[role] = cap

    def pause_source(self, source):  # noqa: ANN001
        self.paused.add(source)


class _AllowPolicy:
    def get_posture(self, name, tool_input):  # noqa: ANN001
        return PermissionResult.PASSTHROUGH


async def _make_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tools=None,
    verb_ask_fn=None,
    yaml_body: str = _VALID_YAML,
    app_slots=None,
) -> TestClient:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    monkeypatch.setenv("TESSERACT_MCP_SECRET", _TOKEN)
    monkeypatch.setenv("TESSERACT_MCP_RESTRICTED", _RESTRICTED_TOKEN)
    reset_activity_registry()
    reset_background_bus()
    (tmp_path / "mcp.yaml").write_text(yaml_body, encoding="utf-8")
    config = load_mcp_config(tmp_path / "mcp.yaml")

    app = web.Application()
    MCPServer.register_routes(app)
    mcp_approvals_route.register(app)
    app["mcp_server"] = MCPServer(config, verb_ask_fn=verb_ask_fn)
    app["repo_root"] = tmp_path
    app["config"] = SimpleNamespace(permissions=_AllowPolicy())
    app["tool_registry"] = SimpleNamespace(tools=tools or {})
    for key, value in (app_slots or {}).items():
        app[key] = value

    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _auth(token: str = _TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _dispatch(client, verb, params=None, *, token=_TOKEN):
    """Dispatcher-direct verb call — the governed core the pruned POST /mcp/call
    handler forwarded verbatim. Returns (http_status, body) exactly as before."""
    from tesseract.mirror.server.mcp.auth import authenticate
    server = client.app["mcp_server"]
    mcp_client = authenticate(server._config, f"Bearer {token}")
    return await server._dispatcher.dispatch(
        client.app, verb, params or {}, mcp_client, ask_fn=server._verb_ask_fn
    )


_SAVE_PARAMS = {"type": "project", "title": "t", "content": "c"}


# ── config loader (trust_tiers) ──────────────────────────────────────────

def test_loader_loads_trust_tiers(tmp_path: Path) -> None:
    (tmp_path / "mcp.yaml").write_text(_VALID_YAML, encoding="utf-8")
    cfg = load_mcp_config(tmp_path / "mcp.yaml")
    assert cfg.trust_tier_cap("operator") == "auto"
    assert cfg.trust_tier_cap("restricted") == "ask"


def test_loader_requires_all_trust_tiers(tmp_path: Path) -> None:
    body = _VALID_YAML.replace('  restricted: "ask"\n', "")
    (tmp_path / "mcp.yaml").write_text(body, encoding="utf-8")
    with pytest.raises(RuntimeError, match="trust_tiers missing"):
        load_mcp_config(tmp_path / "mcp.yaml")


# ── ASK-over-MCP ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ask_verb_without_ask_fn_returns_awaiting_handle(tmp_path, monkeypatch) -> None:
    tool = _FakeTool("memory_save", "SAVED")
    client = await _make_client(tmp_path, monkeypatch, tools={"memory_save": tool})
    try:
        status, body = await _dispatch(client, "memory.save", _SAVE_PARAMS)
        assert status == 202
    finally:
        await client.close()
    assert body["status"] == "awaiting_operator"
    assert body["approval_id"]
    assert tool.ran is False  # nothing executed without approval


@pytest.mark.asyncio
async def test_ask_verb_approved_executes(tmp_path, monkeypatch) -> None:
    tool = _FakeTool("memory_save", "SAVED")

    async def approve(verb, params, mcp_client):  # noqa: ANN001
        return True

    client = await _make_client(
        tmp_path, monkeypatch, tools={"memory_save": tool}, verb_ask_fn=approve
    )
    try:
        status, body = await _dispatch(client, "memory.save", _SAVE_PARAMS)
        assert status == 200
    finally:
        await client.close()
    assert body["data"] == "SAVED"
    assert tool.ran is True


@pytest.mark.asyncio
async def test_ask_verb_declined_is_403(tmp_path, monkeypatch) -> None:
    tool = _FakeTool("memory_save", "SAVED")

    async def decline(verb, params, mcp_client):  # noqa: ANN001
        return False

    client = await _make_client(
        tmp_path, monkeypatch, tools={"memory_save": tool}, verb_ask_fn=decline
    )
    try:
        status, body = await _dispatch(client, "memory.save", _SAVE_PARAMS)
        assert status == 403
    finally:
        await client.close()
    assert tool.ran is False


@pytest.mark.asyncio
async def test_bad_params_are_400_not_500(tmp_path, monkeypatch) -> None:
    # Pydantic v2 ValidationError is not a ValueError; the handler must catch it
    # and surface 400, not let it escape as an unhandled 500.
    tool = _FakeTool("memory_save", "SAVED")

    async def approve(verb, params, mcp_client):  # noqa: ANN001
        return True

    client = await _make_client(
        tmp_path, monkeypatch, tools={"memory_save": tool}, verb_ask_fn=approve
    )
    try:
        status, body = await _dispatch(
            client, "memory.save", {"title": "missing type + content"}
        )
        assert status == 400
    finally:
        await client.close()
    assert tool.ran is False
    # P7 live-gate finding: a lane client guessed at memory.save's param
    # shape and got a bare error_400 three times — the message must name
    # the missing required fields in a terse summary, not just echo
    # pydantic's verbose multi-line per-error dump.
    assert "missing required field(s)" in body["error"]
    assert "type" in body["error"]
    assert "content" in body["error"]


@pytest.mark.asyncio
async def test_verb_denied_by_mcp_posture_is_403(tmp_path, monkeypatch) -> None:
    yaml_body = _VALID_YAML.replace('memory.save: "ask"', 'memory.save: "deny"')
    tool = _FakeTool("memory_save", "SAVED")
    client = await _make_client(
        tmp_path, monkeypatch, tools={"memory_save": tool}, yaml_body=yaml_body
    )
    try:
        status, body = await _dispatch(client, "memory.save", _SAVE_PARAMS)
        assert status == 403
    finally:
        await client.close()
    assert tool.ran is False


# ── trust-tier cap ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_restricted_client_cap_tightens_auto_to_ask(tmp_path, monkeypatch) -> None:
    # activity.list floor is auto; a restricted client's cap is ask, so the
    # effective posture tightens to ask → awaiting_operator (no ask_fn).
    client = await _make_client(tmp_path, monkeypatch)
    try:
        status, body = await _dispatch(client, "activity.list", token=_RESTRICTED_TOKEN)
        assert status == 202
    finally:
        await client.close()
    assert body["status"] == "awaiting_operator"


@pytest.mark.asyncio
async def test_operator_client_reads_activity_auto(tmp_path, monkeypatch) -> None:
    # Same verb, operator client (cap auto) → executes immediately.
    client = await _make_client(tmp_path, monkeypatch)
    try:
        status, body = await _dispatch(client, "activity.list")
        assert status == 200
    finally:
        await client.close()


# ── P3 s2: lane / schedule / surface families ─────────────────────────────

@pytest.mark.asyncio
async def test_mcp_toolcontext_wires_substrate_providers(tmp_path, monkeypatch) -> None:
    # An AUTO tool-backed verb (surface.focus) must run with the substrate
    # providers wired off the app, exactly like the chat path.
    tool = _CtxCapturingTool("surface_focus")
    sched = object()
    client = await _make_client(
        tmp_path,
        monkeypatch,
        tools={"surface_focus": tool},
        app_slots={"scheduler": sched},
    )
    try:
        status, body = await _dispatch(client, "surface.focus", {"surface_id": "s1"})
        assert status == 200
    finally:
        await client.close()
    ctx = tool.captured
    assert ctx is not None
    assert ctx.scheduler_provider() is sched
    assert ctx.lane_manager_provider is not None
    assert ctx.named_lane_manager_provider is not None


@pytest.mark.asyncio
async def test_surface_spawn_maps_to_surface_create_tool(tmp_path, monkeypatch) -> None:
    # Verb name != tool name: surface.spawn → surface_create.
    tool = _FakeTool("surface_create", "CREATED")

    async def approve(verb, params, mcp_client):  # noqa: ANN001
        return True

    client = await _make_client(
        tmp_path, monkeypatch, tools={"surface_create": tool}, verb_ask_fn=approve
    )
    try:
        status, body = await _dispatch(client, "surface.spawn", {"type": "html", "view": "tars"})
        assert status == 200
    finally:
        await client.close()
    assert body["data"] == "CREATED"
    assert tool.ran is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verb, params",
    [
        ("schedule.run", {"name": "job"}),
        ("surface.close", {"surface_id": "s1"}),  # destructive write — must be ASK-gated
    ],
)
async def test_ask_family_verb_returns_handle(tmp_path, monkeypatch, verb, params) -> None:
    # ASK verbs; no ask_fn → awaiting_operator handle, tool never touched.
    client = await _make_client(tmp_path, monkeypatch)
    try:
        status, body = await _dispatch(client, verb, params)
        assert status == 202
    finally:
        await client.close()
    assert body["status"] == "awaiting_operator"


# ── P3 s3: operator approval (approve → resume) ──────────────────────────

async def _await_pending(registry: MCPApprovalRegistry) -> str:
    for _ in range(200):
        pend = registry.pending()
        if pend:
            return pend[0]["approval_id"]
        await asyncio.sleep(0.01)
    raise AssertionError("no pending approval appeared")


@pytest.mark.asyncio
async def test_ask_verb_completes_after_operator_approval(tmp_path, monkeypatch) -> None:
    tool = _FakeTool("schedule_run", "RAN")
    registry = MCPApprovalRegistry()
    ask_fn = build_verb_ask_fn(registry, lambda *a: None, timeout_s=5.0)
    client = await _make_client(
        tmp_path,
        monkeypatch,
        tools={"schedule_run": tool},
        verb_ask_fn=ask_fn,
        app_slots={"mcp_approvals": registry},
    )
    try:
        task = asyncio.create_task(
            _dispatch(client, "schedule.run", {"name": "job"})
        )
        approval_id = await _await_pending(registry)
        dec = await client.post(
            f"/api/mcp/approvals/{approval_id}/decision", json={"approved": True}
        )
        assert dec.status == 200
        status, body = await task
        assert status == 200
    finally:
        await client.close()
    assert body["data"] == "RAN"
    assert tool.ran is True


@pytest.mark.asyncio
async def test_ask_verb_declined_by_operator_is_403(tmp_path, monkeypatch) -> None:
    tool = _FakeTool("schedule_run", "RAN")
    registry = MCPApprovalRegistry()
    ask_fn = build_verb_ask_fn(registry, lambda *a: None, timeout_s=5.0)
    client = await _make_client(
        tmp_path,
        monkeypatch,
        tools={"schedule_run": tool},
        verb_ask_fn=ask_fn,
        app_slots={"mcp_approvals": registry},
    )
    try:
        task = asyncio.create_task(
            _dispatch(client, "schedule.run", {"name": "job"})
        )
        approval_id = await _await_pending(registry)
        await client.post(
            f"/api/mcp/approvals/{approval_id}/decision", json={"approved": False}
        )
        status, body = await task
        assert status == 403
    finally:
        await client.close()
    assert tool.ran is False


@pytest.mark.asyncio
async def test_ask_verb_times_out_to_handle(tmp_path, monkeypatch) -> None:
    registry = MCPApprovalRegistry()
    ask_fn = build_verb_ask_fn(registry, lambda *a: None, timeout_s=0.05)
    client = await _make_client(
        tmp_path, monkeypatch, verb_ask_fn=ask_fn, app_slots={"mcp_approvals": registry}
    )
    try:
        status, body = await _dispatch(client, "schedule.run", {"name": "job"})
        assert status == 202
        assert body["status"] == "awaiting_operator"
        assert body["approval_id"]
        # Timeout is terminal: the approval_id is a correlation token, not
        # resolvable — a late decision on it is 404 (bounded-hold contract).
        dec = await client.post(
            f"/api/mcp/approvals/{body['approval_id']}/decision", json={"approved": True}
        )
        assert dec.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_decide_unknown_approval_is_404(tmp_path, monkeypatch) -> None:
    registry = MCPApprovalRegistry()
    client = await _make_client(tmp_path, monkeypatch, app_slots={"mcp_approvals": registry})
    try:
        resp = await client.post(
            "/api/mcp/approvals/does-not-exist/decision", json={"approved": True}
        )
        assert resp.status == 404
    finally:
        await client.close()


# ── P3 s3: activity.cancel ───────────────────────────────────────────────

async def _approve(verb, params, mcp_client):  # noqa: ANN001
    return True


@pytest.mark.asyncio
async def test_activity_cancel_delegate(tmp_path, monkeypatch) -> None:
    spawns = _FakeSpawns(["h1"])
    sess = SimpleNamespace(chat_session=SimpleNamespace(spawns=spawns))
    client = await _make_client(
        tmp_path, monkeypatch, verb_ask_fn=_approve, app_slots={"server_sessions": {"s1": sess}}
    )
    _register(ActivityRecord(
        activity_id="delegate:h1", kind="delegate", label="claude",
        state="running", durability="ephemeral",
    ))
    try:
        status, body = await _dispatch(client, "activity.cancel", {"activity_id": "delegate:h1"})
        assert status == 200
    finally:
        await client.close()
    assert spawns.cancelled == ["h1"]


@pytest.mark.asyncio
async def test_activity_cancel_lane_reuses_lane_close(tmp_path, monkeypatch) -> None:
    tool = _FakeTool("lane_close", "closed")
    client = await _make_client(
        tmp_path, monkeypatch, tools={"lane_close": tool}, verb_ask_fn=_approve
    )
    _register(ActivityRecord(
        activity_id="lane:L1", kind="lane", label="claude",
        state="running", durability="persistent",
    ))
    try:
        status, body = await _dispatch(client, "activity.cancel", {"activity_id": "lane:L1"})
        assert status == 200
    finally:
        await client.close()
    assert tool.ran is True


@pytest.mark.asyncio
async def test_activity_cancel_unsupported_kind_is_400(tmp_path, monkeypatch) -> None:
    client = await _make_client(tmp_path, monkeypatch, verb_ask_fn=_approve)
    _register(ActivityRecord(
        activity_id="session:s1", kind="controller_session", label="tars",
        state="running", durability="persistent",
    ))
    try:
        status, body = await _dispatch(client, "activity.cancel", {"activity_id": "session:s1"})
        assert status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_activity_cancel_mcp_session_not_connected_is_404(tmp_path, monkeypatch) -> None:
    client = await _make_client(tmp_path, monkeypatch, verb_ask_fn=_approve)
    _register(ActivityRecord(
        activity_id="mcp:op:abc", kind="mcp_session", label="MCP",
        state="running", durability="ephemeral",
    ))
    try:
        status, body = await _dispatch(client, "activity.cancel", {"activity_id": "mcp:op:abc"})
        assert status == 404  # record exists but no live SSE connection
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_activity_cancel_unknown_is_404(tmp_path, monkeypatch) -> None:
    client = await _make_client(tmp_path, monkeypatch, verb_ask_fn=_approve)
    try:
        status, body = await _dispatch(client, "activity.cancel", {"activity_id": "delegate:nope"})
        assert status == 404
    finally:
        await client.close()


# ── P3 s3: budget family ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_budget_status_returns_summary(tmp_path, monkeypatch) -> None:
    ledger = _FakeLedger()
    client = await _make_client(tmp_path, monkeypatch, app_slots={"cost_ledger": ledger})
    try:
        status, body = await _dispatch(client, "budget.status")
        assert status == 200
    finally:
        await client.close()
    assert body["data"]["cap_usd"] == 10.0
    assert body["data"]["spent_usd"] == 1.0


@pytest.mark.asyncio
async def test_budget_set_cap_requires_approval(tmp_path, monkeypatch) -> None:
    ledger = _FakeLedger()
    client = await _make_client(tmp_path, monkeypatch, app_slots={"cost_ledger": ledger})
    try:
        status, body = await _dispatch(
            client, "budget.set_cap", {"role": "chat_brain", "cap_usd": 5.0}
        )
        assert status == 202  # ASK, no ask_fn → handle; cap NOT changed
    finally:
        await client.close()
    assert ledger.caps == {}


@pytest.mark.asyncio
async def test_budget_set_cap_applies_after_approval(tmp_path, monkeypatch) -> None:
    ledger = _FakeLedger()

    async def approve(verb, params, mcp_client):  # noqa: ANN001
        return True

    client = await _make_client(
        tmp_path, monkeypatch, verb_ask_fn=approve, app_slots={"cost_ledger": ledger}
    )
    try:
        status, body = await _dispatch(
            client, "budget.set_cap", {"role": "chat_brain", "cap_usd": 5.0}
        )
        assert status == 200
    finally:
        await client.close()
    assert ledger.caps == {"chat_brain": 5.0}


# ── P3 s3: agent family ──────────────────────────────────────────────────

class _FakeSessionRegistry:
    """Stand-in for SessionRegistry — agent.status/review read a session record."""

    def __init__(self, records) -> None:
        self._records = records  # {session_id: (dump_dict, transcript_path)}

    def get_session(self, session_id):  # noqa: ANN001
        entry = self._records.get(session_id)
        if entry is None:
            return None
        dump, tpath = entry
        return SimpleNamespace(model_dump=lambda: dump, transcript_path=tpath)


@pytest.mark.asyncio
async def test_agent_assign_requires_approval(tmp_path, monkeypatch) -> None:
    tool = _FakeTool("start_controller_session", "sess-123")
    client = await _make_client(tmp_path, monkeypatch, tools={"start_controller_session": tool})
    try:
        status, body = await _dispatch(client, "agent.assign", {"task": "do X"})
        assert status == 202  # ASK, no ask_fn
    finally:
        await client.close()
    assert tool.ran is False


@pytest.mark.asyncio
async def test_agent_assign_dispatches_after_approval(tmp_path, monkeypatch) -> None:
    tool = _FakeTool("start_controller_session", "sess-123")
    client = await _make_client(
        tmp_path, monkeypatch, tools={"start_controller_session": tool}, verb_ask_fn=_approve
    )
    try:
        status, body = await _dispatch(client, "agent.assign", {"task": "do X"})
        assert status == 200
    finally:
        await client.close()
    assert body["data"] == "sess-123"
    assert tool.ran is True


@pytest.mark.asyncio
async def test_agent_status_returns_record(tmp_path, monkeypatch) -> None:
    fake = _FakeSessionRegistry({"S1": ({"session_id": "S1", "status": "active"}, "")})
    monkeypatch.setattr(
        "tesseract.mirror.server.mcp.verbs.agent.SessionRegistry", lambda: fake
    )
    client = await _make_client(tmp_path, monkeypatch)
    try:
        status, body = await _dispatch(client, "agent.status", {"session_id": "S1"})
        assert status == 200
    finally:
        await client.close()
    assert body["data"]["session_id"] == "S1"


@pytest.mark.asyncio
async def test_agent_status_unknown_is_404(tmp_path, monkeypatch) -> None:
    fake = _FakeSessionRegistry({})
    monkeypatch.setattr(
        "tesseract.mirror.server.mcp.verbs.agent.SessionRegistry", lambda: fake
    )
    client = await _make_client(tmp_path, monkeypatch)
    try:
        status, body = await _dispatch(client, "agent.status", {"session_id": "nope"})
        assert status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_agent_status_corrupt_record_is_400(tmp_path, monkeypatch) -> None:
    # A corrupt/partial on-disk record raises pydantic ValidationError from
    # get_session — must surface 400, not an unhandled 500.
    from tesseract.orchestrator.tars_controller.sessions import ControllerSessionRecord

    class _RaisingReg:
        def get_session(self, session_id):  # noqa: ANN001
            return ControllerSessionRecord.model_validate({})  # missing required → ValidationError

    monkeypatch.setattr(
        "tesseract.mirror.server.mcp.verbs.agent.SessionRegistry", lambda: _RaisingReg()
    )
    client = await _make_client(tmp_path, monkeypatch)
    try:
        status, body = await _dispatch(client, "agent.status", {"session_id": "S1"})
        assert status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_agent_status_missing_session_id_is_400(tmp_path, monkeypatch) -> None:
    client = await _make_client(tmp_path, monkeypatch)
    try:
        status, body = await _dispatch(client, "agent.status")
        assert status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_agent_review_returns_transcript_events(tmp_path, monkeypatch) -> None:
    tfile = tmp_path / "t.jsonl"
    tfile.write_text('{"event":"a"}\n{"event":"b"}\n', encoding="utf-8")
    fake = _FakeSessionRegistry({"S1": ({"session_id": "S1"}, str(tfile))})
    monkeypatch.setattr(
        "tesseract.mirror.server.mcp.verbs.agent.SessionRegistry", lambda: fake
    )
    client = await _make_client(tmp_path, monkeypatch)
    try:
        status, body = await _dispatch(client, "agent.review", {"session_id": "S1"})
        assert status == 200
    finally:
        await client.close()
    assert body["data"]["event_count"] == 2
    assert body["data"]["events"] == [{"event": "a"}, {"event": "b"}]


# ── audit sink ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verb_call_writes_audit_row(tmp_path, monkeypatch) -> None:
    client = await _make_client(tmp_path, monkeypatch)
    try:
        await _dispatch(client, "activity.list")
    finally:
        await client.close()
    path = mcp_audit_path()
    assert path.exists()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["verb"] == "activity.list"
    assert rows[-1]["client"] == "operator"
    assert rows[-1]["decision"] == "ok"
