"""AU-3 — Tool.risk_class ClassVar is mandatory + valid.

Boot asserts every concrete Tool subclass declares risk_class as one of
the four taxonomy values. A missing or invalid value raises at
``_wire_tool_defaults`` time. This test covers the validator in
isolation (no full boot needed)."""

from __future__ import annotations

from typing import ClassVar

import pytest
from pydantic import BaseModel

from tesseract.brain.boot import _wire_tool_defaults
from tesseract.brain.tools import ToolRegistry
from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult


def _make_tool_class(*, posture: str, risk: str | None) -> type[Tool]:
    class _Input(BaseModel):
        pass

    body: dict = {
        "default_posture": posture,
        "name": property(lambda self: "doe_probe"),
        "description": property(lambda self: "doe"),
        "input_schema": property(lambda self: _Input),
    }
    if risk is not None:
        body["risk_class"] = risk

    async def _run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        return ToolResult(output="ok")

    body["run"] = _run
    # Add ClassVar typing for static-checker parity with production tools.
    body["__annotations__"] = {
        "default_posture": ClassVar[str],
    }
    if risk is not None:
        body["__annotations__"]["risk_class"] = ClassVar[str]

    return type("_DoeProbeTool", (Tool,), body)


def _register_only(cls: type[Tool]) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(cls())
    return reg


def test_valid_risk_class_passes() -> None:
    cls = _make_tool_class(posture="auto", risk="autonomous")
    _wire_tool_defaults(_register_only(cls), policy=None)


def test_missing_risk_class_raises() -> None:
    cls = _make_tool_class(posture="auto", risk=None)
    with pytest.raises(RuntimeError, match="risk_class"):
        _wire_tool_defaults(_register_only(cls), policy=None)


def test_invalid_risk_class_raises() -> None:
    cls = _make_tool_class(posture="auto", risk="banana")
    with pytest.raises(RuntimeError, match="risk_class"):
        _wire_tool_defaults(_register_only(cls), policy=None)


@pytest.mark.parametrize(
    "risk",
    ["autonomous", "propose", "operator_gate", "absolute_deny"],
)
def test_all_four_taxonomy_values_accepted(risk: str) -> None:
    cls = _make_tool_class(posture="auto", risk=risk)
    _wire_tool_defaults(_register_only(cls), policy=None)
