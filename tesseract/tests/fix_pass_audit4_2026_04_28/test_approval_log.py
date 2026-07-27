"""Audit-4 P1 — durable approval ledger writes one JSONL row per
permission decision and never interleaves bytes under concurrency.

Pins:
  - ``approval_log.record_ask`` emits one line with every required field.
  - Hard-DENY paths (security gate, path validator, policy default deny)
    record a ``deny``/``system`` row from inside ``decide.evaluate``.
  - ASK posture without a wired ``ask_fn`` records ``allow_once``/
    ``system`` for read-only tools and ``deny``/``system`` for write
    tools.
  - Concurrent calls produce N complete lines — no torn writes.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import (
    PermissionResult,
    Tool,
    ToolContext,
    ToolResult,
)
from tesseract.permissions import approval_log, decide
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


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


def _ctx(home: Path, *, session_id: str = "s", call_id: str = "c1") -> ToolContext:
    return ToolContext(
        workspace_root=str(home),
        session_id=session_id,
        current_call_id=call_id,
    )


def _read_ledger(home: Path) -> list[dict]:
    path = home / "logs" / "approvals.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_record_ask_writes_one_line_with_all_fields(home: Path) -> None:
    asyncio.run(approval_log.record_ask(
        session_id="s1",
        call_id="c1",
        tool_name="bash",
        input_summary={"command": "ls"},
        posture_source="default",
        result="allow_once",
        actor="operator",
    ))
    rows = _read_ledger(home)
    assert len(rows) == 1
    row = rows[0]
    for key in ("ts", "session_id", "call_id", "tool", "input_summary", "posture_source", "result", "actor"):
        assert key in row
    assert row["session_id"] == "s1"
    assert row["call_id"] == "c1"
    assert row["tool"] == "bash"
    assert row["posture_source"] == "default"
    assert row["result"] == "allow_once"
    assert row["actor"] == "operator"


def test_security_deny_records_system_deny_row(home: Path) -> None:
    tool = _StubTool(name="t_sec", check_result=PermissionResult.DENY)
    out = asyncio.run(decide.evaluate(
        tool=tool, validated=_NopInput(), raw_input={},
        context=_ctx(home), ask_fn=None, policy=None,
    ))
    assert out is not None and out.denied_hard
    rows = _read_ledger(home)
    assert len(rows) == 1
    assert rows[0]["posture_source"] == "security"
    assert rows[0]["result"] == "deny"
    assert rows[0]["actor"] == "system"
    assert rows[0]["tool"] == "t_sec"


def test_path_validator_deny_records_path_validator_row(home: Path) -> None:
    tool = _StubTool(
        name="file_write",
        check_result=PermissionResult.PASSTHROUGH,
        input_cls=_PathInput,
    )
    bad = "..\\escape\\file.txt"
    out = asyncio.run(decide.evaluate(
        tool=tool,
        validated=_PathInput(file_path=bad),
        raw_input={"file_path": bad},
        context=_ctx(home),
        ask_fn=None,
        policy=None,
    ))
    assert out is not None and out.denied_hard
    rows = _read_ledger(home)
    assert len(rows) == 1
    assert rows[0]["posture_source"] == "path_validator"
    assert rows[0]["result"] == "deny"
    assert rows[0]["actor"] == "system"


def test_ask_no_channel_write_tool_records_system_deny(home: Path) -> None:
    tool = _StubTool(name="t_ask_w", check_result=PermissionResult.ASK, read_only=False)
    out = asyncio.run(decide.evaluate(
        tool=tool, validated=_NopInput(), raw_input={},
        context=_ctx(home), ask_fn=None, policy=None,
    ))
    assert out is not None and out.denied_hard
    rows = _read_ledger(home)
    assert len(rows) == 1
    assert rows[0]["result"] == "deny"
    assert rows[0]["actor"] == "system"
    # tool.check_permissions returned ASK directly → posture_source="tool".
    assert rows[0]["posture_source"] == "tool"


def test_ask_no_channel_read_only_tool_records_system_allow(home: Path) -> None:
    tool = _StubTool(name="t_ask_r", check_result=PermissionResult.ASK, read_only=True)
    out = asyncio.run(decide.evaluate(
        tool=tool, validated=_NopInput(), raw_input={},
        context=_ctx(home), ask_fn=None, policy=None,
    ))
    # Read-only ASK with no channel auto-allows; evaluate returns None.
    assert out is None
    rows = _read_ledger(home)
    assert len(rows) == 1
    assert rows[0]["result"] == "allow_once"
    assert rows[0]["actor"] == "system"


def test_ask_with_channel_records_operator_allow(home: Path) -> None:
    tool = _StubTool(name="t_ask_op", check_result=PermissionResult.ASK, read_only=False)

    async def approve(_tool, _validated, _ctx_arg) -> bool:
        # Simulate the Mirror/REPL ask_fn writing its own ledger row.
        await approval_log.record_ask(
            session_id=_ctx_arg.session_id,
            call_id=_ctx_arg.current_call_id,
            tool_name=_tool.name,
            input_summary={},
            posture_source=_ctx_arg.posture_source or "default",
            result="allow_once",
            actor="operator",
        )
        return True

    out = asyncio.run(decide.evaluate(
        tool=tool, validated=_NopInput(), raw_input={},
        context=_ctx(home), ask_fn=approve, policy=None,
    ))
    assert out is None  # proceed
    rows = _read_ledger(home)
    assert len(rows) == 1
    assert rows[0]["result"] == "allow_once"
    assert rows[0]["actor"] == "operator"


def test_concurrent_writes_produce_complete_lines(home: Path) -> None:
    summary = approval_log.summarize_input({"command": "x" * 800})

    async def driver() -> None:
        await asyncio.gather(*[
            approval_log.record_ask(
                session_id="s",
                call_id=f"c{i}",
                tool_name="bash",
                input_summary=summary,
                posture_source="default",
                result="allow_once",
                actor="operator",
            )
            for i in range(20)
        ])

    asyncio.run(driver())
    rows = _read_ledger(home)
    assert len(rows) == 20
    call_ids = {r["call_id"] for r in rows}
    assert call_ids == {f"c{i}" for i in range(20)}
    for r in rows:
        assert "<truncated 300 chars>" in r["input_summary"]["command"]


def test_summarize_input_truncates_long_strings() -> None:
    big = "y" * 700
    out = approval_log.summarize_input({"command": big, "n": 5, "nested": {"k": big}})
    assert out["n"] == 5
    assert "<truncated 200 chars>" in out["command"]
    assert "<truncated 200 chars>" in out["nested"]["k"]


def test_resolve_posture_source_path_match(home: Path) -> None:
    policy = PermissionPolicy(
        tools_defaults={"file_write": "auto"},
        modes={},
        path_overrides={
            "file_write": [
                {"path_prefix": "secret/", "posture": "deny"},
            ]
        },
        current_mode="max",
        workspace_root=str(home),
    )
    src = decide._resolve_posture_source(
        policy, "file_write", {"file_path": "secret/key.txt"}
    )
    assert src == "path"


def test_resolve_posture_source_default_when_no_overrides(home: Path) -> None:
    policy = PermissionPolicy(
        tools_defaults={"bash": "ask"},
        modes={},
        path_overrides={},
        current_mode="max",
        workspace_root=str(home),
    )
    src = decide._resolve_posture_source(policy, "bash", {})
    assert src == "default"
