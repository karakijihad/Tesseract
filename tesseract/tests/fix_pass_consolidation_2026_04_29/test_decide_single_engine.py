"""Permissions consolidation (2026-04-29) — `permissions/decide.py` is
the single decision pipeline. The dead `PermissionEngine` /
`PermissionDecision` / `AskRules` / `DenyRules` / `ApprovalLog` modules
were deleted; `brain/tools.py::execute_tool` is a thin wrapper around
`decide.evaluate`.

These tests pin the contract:
  - `decide.evaluate` returns `None` to mean "proceed" (caller runs the
    tool); returns a `ToolResult` to mean "do NOT run the tool".
  - The four denial paths each return a typed `ToolResult` with
    `denied_hard=True` (security DENY, path validator, policy DENY,
    ASK without ask_fn for non-read-only tools).
  - The deleted modules stay deleted — guards against accidental
    resurrection.
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import (
    PermissionResult,
    Tool,
    ToolContext,
    ToolResult,
)
from tesseract.permissions import decide
from tesseract.permissions.policy import PermissionPolicy


class _NopInput(BaseModel):
    pass


class _PathInput(BaseModel):
    file_path: str = Field(default="")


class _StubTool(Tool):
    def __init__(
        self,
        *,
        name: str,
        check_result: PermissionResult,
        read_only: bool = False,
        input_cls: type[BaseModel] = _NopInput,
    ) -> None:
        self._name = name
        self._check = check_result
        self._read_only = read_only
        self._input_cls = input_cls

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "stub"

    @property
    def input_schema(self) -> type[BaseModel]:
        return self._input_cls

    def is_read_only(self) -> bool:
        return self._read_only

    def is_concurrency_safe(self) -> bool:
        return True

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return self._check

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        return ToolResult(output="ran")


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(workspace_root=str(tmp_path))


def test_evaluate_returns_none_when_passthrough(tmp_path: Path) -> None:
    tool = _StubTool(name="t_ok", check_result=PermissionResult.PASSTHROUGH)
    out = asyncio.run(decide.evaluate(
        tool=tool, validated=_NopInput(), raw_input={},
        context=_ctx(tmp_path), ask_fn=None, policy=None,
    ))
    assert out is None


def test_evaluate_security_deny_returns_denied_hard(tmp_path: Path) -> None:
    tool = _StubTool(name="t_block", check_result=PermissionResult.DENY)
    out = asyncio.run(decide.evaluate(
        tool=tool, validated=_NopInput(), raw_input={},
        context=_ctx(tmp_path), ask_fn=None, policy=None,
    ))
    assert isinstance(out, ToolResult)
    assert out.denied_hard
    assert "security layer" in (out.deny_reason or "")


def test_evaluate_path_validator_blocks_workspace_escape(tmp_path: Path) -> None:
    tool = _StubTool(
        name="file_write",
        check_result=PermissionResult.PASSTHROUGH,
        input_cls=_PathInput,
    )
    out = asyncio.run(decide.evaluate(
        tool=tool, validated=_PathInput(file_path="../../etc/passwd"),
        raw_input={"file_path": "../../etc/passwd"},
        context=_ctx(tmp_path), ask_fn=None, policy=None,
    ))
    assert isinstance(out, ToolResult)
    assert out.denied_hard
    assert "path_validator" in (out.deny_reason or "")


def test_evaluate_policy_deny(tmp_path: Path) -> None:
    class _DenyAll(PermissionPolicy):
        def __init__(self) -> None:
            pass

        def get_posture(self, tool_name, validated):
            return PermissionResult.DENY

    tool = _StubTool(name="t_anything", check_result=PermissionResult.PASSTHROUGH)
    out = asyncio.run(decide.evaluate(
        tool=tool, validated=_NopInput(), raw_input={},
        context=_ctx(tmp_path), ask_fn=None, policy=_DenyAll(),
    ))
    assert isinstance(out, ToolResult)
    assert out.denied_hard
    assert "policy default deny" in (out.deny_reason or "")


def test_evaluate_ask_with_no_ask_fn_denies_write(tmp_path: Path) -> None:
    tool = _StubTool(name="t_write_ask", check_result=PermissionResult.ASK, read_only=False)
    out = asyncio.run(decide.evaluate(
        tool=tool, validated=_NopInput(), raw_input={},
        context=_ctx(tmp_path), ask_fn=None, policy=None,
    ))
    assert isinstance(out, ToolResult)
    assert out.denied_hard
    assert "ASK posture with no approval channel" in (out.deny_reason or "")


def test_evaluate_ask_with_no_ask_fn_allows_read(tmp_path: Path) -> None:
    tool = _StubTool(name="t_read_ask", check_result=PermissionResult.ASK, read_only=True)
    out = asyncio.run(decide.evaluate(
        tool=tool, validated=_NopInput(), raw_input={},
        context=_ctx(tmp_path), ask_fn=None, policy=None,
    ))
    assert out is None


def test_evaluate_ask_operator_decline_returns_error_not_denied_hard(tmp_path: Path) -> None:
    tool = _StubTool(name="t_write_ask", check_result=PermissionResult.ASK, read_only=False)

    async def say_no(tool, validated, context):
        return False

    out = asyncio.run(decide.evaluate(
        tool=tool, validated=_NopInput(), raw_input={},
        context=_ctx(tmp_path), ask_fn=say_no, policy=None,
    ))
    assert isinstance(out, ToolResult)
    assert out.is_error
    assert not out.denied_hard
    assert "declined" in out.output.lower()


@pytest.mark.parametrize("dead_module", [
    "tesseract.permissions.engine",
    "tesseract.permissions.decision",
    "tesseract.permissions.ask_rules",
    "tesseract.permissions.deny_rules",
])
def test_dead_modules_stay_deleted(dead_module: str) -> None:
    """Resurrection guard — the consolidation deleted these modules and
    they must not come back. If a future change re-imports the surface,
    this test fails fast.

    Note: ``approval_log`` was deleted in this consolidation (it held
    in-memory lookup helpers) and intentionally restored under audit-4
    P1 with a different purpose — a durable JSONL ledger. The new
    module's surface is unrelated to the old one. See
    ``tests/fix_pass_audit4_2026_04_28/test_approval_log.py`` for the
    new contract.
    """
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(dead_module)
