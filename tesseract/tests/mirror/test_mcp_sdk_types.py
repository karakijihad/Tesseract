"""Phase 2b — inbound MCP server payloads are built from the official SDK's
validated ``mcp.types`` models (not hand-rolled dicts).

Behavior-preserving: the wire shapes are identical to the pre-migration server
(the full parity suite in ``test_mcp_protocol.py`` still passes unchanged).
These tests assert the *mechanism* — that ``tools.list_tools`` yields
``types.Tool`` models and that every emitted payload round-trips back through
its SDK model, i.e. it is genuinely spec-valid, which a hand-assembled dict
could silently drift away from.
"""

from __future__ import annotations

import mcp.types as types

from tesseract.mirror.server.mcp import protocol
from tesseract.mirror.server.mcp.tools import call_result, list_tools


class _AllowDispatcher:
    """Minimal dispatcher stub: nothing is DENY, so every verb is listed."""

    def resolve_posture(self, verb, client):  # noqa: ANN001
        return "auto"


def test_list_tools_returns_sdk_tool_models() -> None:
    tools = list_tools(_AllowDispatcher(), client=None)
    assert tools, "expected the governed verb catalog to be non-empty"
    assert all(isinstance(t, types.Tool) for t in tools)
    # ListToolsResult (the wire wrapper protocol.py uses) validates the set and
    # dumps to the canonical {"tools": [...]} shape with camelCase aliases.
    dumped = types.ListToolsResult(tools=tools).model_dump(by_alias=True, exclude_none=True)
    assert set(dumped) == {"tools"}
    assert all("inputSchema" in entry for entry in dumped["tools"])


def test_call_result_ok_roundtrips_through_sdk_model() -> None:
    wire = call_result(200, {"data": "MEMORY-HIT"})
    # Round-trip proves the emitted dict is a spec-valid CallToolResult.
    model = types.CallToolResult.model_validate(wire)
    assert model.isError is False
    assert model.content[0].text == "MEMORY-HIT"


def test_call_result_error_paths_are_iserror() -> None:
    pending = types.CallToolResult.model_validate(call_result(202, {"approval_id": "ap-1"}))
    assert pending.isError is True
    assert "awaiting_operator" in pending.content[0].text
    denied = types.CallToolResult.model_validate(call_result(403, {"code": 403, "error": "nope"}))
    assert denied.isError is True


def test_initialize_result_is_spec_valid_via_sdk_model() -> None:
    result = types.InitializeResult(
        protocolVersion=protocol._LATEST_PROTOCOL,
        capabilities=types.ServerCapabilities(tools=types.ToolsCapability(listChanged=False)),
        serverInfo=types.Implementation(name="tesseract", version="1.0"),
    ).model_dump(by_alias=True, exclude_none=True)
    # Same wire shape the pre-migration server produced by hand.
    assert result["protocolVersion"] == protocol._LATEST_PROTOCOL
    assert result["capabilities"]["tools"]["listChanged"] is False
    assert result["serverInfo"] == {"name": "tesseract", "version": "1.0"}
    # And it validates back through the model.
    types.InitializeResult.model_validate(result)


def test_standard_error_codes_match_sdk_constants() -> None:
    # The named constants are numerically the prior literals (wire unchanged).
    assert protocol._INVALID_REQUEST == types.INVALID_REQUEST == -32600
    assert protocol._METHOD_NOT_FOUND == types.METHOD_NOT_FOUND == -32601
    assert protocol._INVALID_PARAMS == types.INVALID_PARAMS == -32602
    # server-error stays the implementation-defined literal (no SDK constant).
    assert protocol._SERVER_ERROR == -32000
