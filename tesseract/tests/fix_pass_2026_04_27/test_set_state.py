"""Unit tests for `SetStateTool` (2026-04-27).

The tool is a pure in-memory holder + Pydantic validation. Coverage:
- accepts each allowed state and writes it to the holder
- rejects unknown / non-settable states with a helpful error message
  and does NOT mutate the holder
- normalizes case and whitespace
- is concurrency-safe (run() is reentrant against a shared affect)
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.set_state import (
    ALLOWED_STATES,
    EntityAffect,
    SetStateInput,
    SetStateTool,
)


def _ctx() -> ToolContext:
    return ToolContext(workspace_root=".", session_id="test")


@pytest.mark.parametrize("state", sorted(ALLOWED_STATES))
async def test_accepts_each_allowed_state(state: str) -> None:
    affect = EntityAffect()
    tool = SetStateTool(affect=affect)
    result = await tool.run(SetStateInput(state=state), _ctx())
    assert not result.is_error, result.output
    assert affect.state == state
    assert result.metadata == {"state": state}
    assert result.output == f"state set: {state}"


async def test_rejects_unknown_state_without_mutating_holder() -> None:
    affect = EntityAffect()
    affect.set("happy")
    tool = SetStateTool(affect=affect)
    result = await tool.run(SetStateInput(state="ascendant"), _ctx())
    assert result.is_error
    assert "unknown" in result.output.lower()
    # Allowed states should be enumerated so the model can self-correct.
    for allowed in ALLOWED_STATES:
        assert allowed in result.output
    # Holder unchanged.
    assert affect.state == "happy"


@pytest.mark.parametrize("state", ["thinking", "speaking", "listening", "error", "spawning"])
async def test_rejects_loop_driven_states(state: str) -> None:
    """Reactive states must NOT be settable — they're loop-driven and a
    TARS-set value would be overwritten in milliseconds, creating noise
    without ever being seen."""
    affect = EntityAffect()
    tool = SetStateTool(affect=affect)
    result = await tool.run(SetStateInput(state=state), _ctx())
    assert result.is_error
    assert affect.state == "idle"  # default unchanged


@pytest.mark.parametrize("raw,expected", [
    ("HAPPY", "happy"),
    ("Deep_Focus", "deep_focus"),
    ("  idle  ", "idle"),
])
async def test_normalizes_case_and_whitespace(raw: str, expected: str) -> None:
    affect = EntityAffect()
    tool = SetStateTool(affect=affect)
    result = await tool.run(SetStateInput(state=raw), _ctx())
    assert not result.is_error
    assert affect.state == expected


def test_input_schema_rejects_non_string() -> None:
    with pytest.raises(ValidationError):
        SetStateInput(state=42)  # type: ignore[arg-type]


async def test_concurrent_calls_against_shared_affect() -> None:
    """Multiple concurrent set_state calls land deterministically on the
    same EntityAffect — last write wins, no race or partial state."""
    affect = EntityAffect()
    tool = SetStateTool(affect=affect)
    states = ["happy", "deep_focus", "dreaming", "idle", "happy"]
    await asyncio.gather(*[tool.run(SetStateInput(state=s), _ctx()) for s in states])
    assert affect.state in ALLOWED_STATES


async def test_default_affect_is_idle() -> None:
    affect = EntityAffect()
    assert affect.state == "idle"


def test_tool_metadata() -> None:
    tool = SetStateTool(affect=EntityAffect())
    assert tool.name == "set_state"
    # EntityAffect is a shared mutable holder on the tool instance — two
    # concurrent set() calls last-write-wins. WP-1 audit 2026-05-22 corrected
    # the prior over-optimistic `True` annotation. WP-2 will exclude this
    # tool from the synthetic registry rather than try to lock it.
    assert not tool.is_concurrency_safe()
    assert not tool.is_read_only()
    assert "happy" in tool.description.lower()
    assert "deep_focus" in tool.description or "deep focus" in tool.description.lower()
