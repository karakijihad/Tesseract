"""Lean-agent-os P1 Task 2 — tool-schema tiering.

Covers:
  1. `ToolRegistry.schemas_for_adapter` returns core-only at session start.
  2. `tool_search` surfaces + enables matching extended tools for the rest
     of the session (next `schemas_for_adapter` call includes them).
  3. An extended tool still executes when invoked by name without a prior
     `tool_search` — tiering is visibility-only, not a permission gate.
  4. Every tool the live registry (`build_tool_registry`) produces resolves
     a valid `tier` ("core" or "extended").
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import BaseModel

from tesseract.brain.tools import ToolRegistry, execute_tool
from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult
from tesseract.kernel.tools.tool_search import ToolSearchTool


class _EmptyInput(BaseModel):
    pass


class _CoreWidgetTool(Tool):
    """Fixture stand-in for a "core" tool — always visible."""

    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"
    tier: ClassVar[str] = "core"

    @property
    def name(self) -> str:
        return "widget_core_thing"

    @property
    def description(self) -> str:
        return "Always-visible core widget tool."

    @property
    def input_schema(self) -> type[BaseModel]:
        return _EmptyInput

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        return ToolResult(output="core widget ran")


class _ExtendedMissionWidgetTool(Tool):
    """Fixture stand-in for an "extended" tool discoverable via a mission-ish query."""

    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"
    # tier left at the Tool base default ("extended") deliberately.

    @property
    def name(self) -> str:
        return "widget_mission_thing"

    @property
    def description(self) -> str:
        return "Runs a mission-planning widget task."

    @property
    def input_schema(self) -> type[BaseModel]:
        return _EmptyInput

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        return ToolResult(output="mission widget ran")


class _ExtendedOtherWidgetTool(Tool):
    """Fixture stand-in for an unrelated "extended" tool — must NOT match
    a "mission"-keyword search."""

    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"

    @property
    def name(self) -> str:
        return "widget_other_thing"

    @property
    def description(self) -> str:
        return "Does something entirely unrelated."

    @property
    def input_schema(self) -> type[BaseModel]:
        return _EmptyInput

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        return ToolResult(output="other widget ran")


def _build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_CoreWidgetTool())
    registry.register(_ExtendedMissionWidgetTool())
    registry.register(_ExtendedOtherWidgetTool())
    registry.register(ToolSearchTool())
    return registry


def test_schemas_for_adapter_core_only_at_session_start() -> None:
    registry = _build_registry()

    # Fresh session: enabled_extended starts empty, mirroring
    # ChatSession._enabled_extended_tools at construction.
    schemas = registry.schemas_for_adapter(enabled_extended=set())
    names = {s["name"] for s in schemas}

    assert "widget_core_thing" in names
    assert "tool_search" in names
    assert "widget_mission_thing" not in names
    assert "widget_other_thing" not in names


def test_schemas_for_adapter_default_returns_everything() -> None:
    """No `enabled_extended` arg (capability-matrix / introspection callers)
    must keep seeing the full registry, unfiltered by tier."""
    registry = _build_registry()
    names = {s["name"] for s in registry.schemas_for_adapter()}
    assert names == {
        "widget_core_thing",
        "widget_mission_thing",
        "widget_other_thing",
        "tool_search",
    }


@pytest.mark.asyncio
async def test_tool_search_enables_matching_extended_tool_for_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    registry = _build_registry()
    enabled: set[str] = set()
    context = ToolContext(
        workspace_root=str(tmp_path),
        session_id="test-session",
        current_call_id="call-1",
        tool_registry_provider=lambda: registry,
        enabled_extended_tools=enabled,
    )

    result = await execute_tool(
        registry, "tool_search", {"query": "mission"}, context
    )

    assert result.is_error is False
    assert "widget_mission_thing" in result.output
    assert "widget_other_thing" not in result.output
    assert enabled == {"widget_mission_thing"}

    # The next adapter call (next turn in the session) now includes it —
    # session-enablement, not a one-shot response.
    schemas = registry.schemas_for_adapter(enabled_extended=enabled)
    names = {s["name"] for s in schemas}
    assert "widget_mission_thing" in names
    assert "widget_other_thing" not in names


@pytest.mark.asyncio
async def test_extended_tool_executes_without_prior_tool_search(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Invariant: tiering is visibility-only. An extended tool invoked by
    name resolves and runs even though no `tool_search` call ever
    surfaced it — the registry lookup + permission decision are
    tier-blind."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    registry = _build_registry()
    context = ToolContext(
        workspace_root=str(tmp_path),
        session_id="test-session",
        current_call_id="call-2",
        tool_registry_provider=lambda: registry,
        enabled_extended_tools=set(),
    )

    result = await execute_tool(registry, "widget_other_thing", {}, context)

    assert result.is_error is False
    assert result.output == "other widget ran"


@pytest.mark.asyncio
async def test_every_registered_tool_has_a_valid_tier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end sanity check against the live boot registry: every tool
    `build_tool_registry` produces must resolve `tier` to "core" or
    "extended" (mirrors the RuntimeError guard in
    `boot._wire_tool_defaults`), and the tiering actually narrows the
    per-turn surface (core is a strict, non-trivial subset)."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    monkeypatch.setattr("tesseract.paths.TESSERACT_HOME", tmp_path)
    monkeypatch.setattr("tesseract.brain.boot.TESSERACT_HOME", tmp_path)

    from tesseract.brain.boot import build_tool_registry

    registry, _mood, _voice, _bundle, _alarms = build_tool_registry()

    tiers = {name: tool.tier for name, tool in registry.tools.items()}
    invalid = {n: t for n, t in tiers.items() if t not in ("core", "extended")}
    assert not invalid, f"tools with invalid tier: {invalid}"

    core_names = {n for n, t in tiers.items() if t == "core"}
    assert "tool_search" in core_names
    assert "memory_search" in core_names
    # Re-sized 2026-07-12 (operator directive: every rule-card-instructed
    # tool is core — ~73 today). Loose band so an incidental addition/
    # removal doesn't flake this test, while still catching the tier filter
    # doing nothing (== len(registry.tools)) or doing everything (== 0).
    assert 50 <= len(core_names) <= 90
    assert len(core_names) < len(registry.tools)

    # A representative rarely-needed tool stays extended (not force-marked
    # core), proving the filter actually excludes something.
    assert "vault_ingest" not in core_names

    # Regression guard: `_apply_tool_tiers` marks the pinned core set via
    # INSTANCE attribute assignment (`registry.tools[name].tier = "core"`),
    # not a class-body edit — `schemas_for_adapter` and `tool_search` MUST
    # read the instance attribute (`getattr(tool, "tier", ...)`), not the
    # class attribute (`getattr(type(tool), "tier", ...)`), or every
    # boot-registered core tool would be invisible in a tiered session.
    schemas = registry.schemas_for_adapter(enabled_extended=set())
    visible_names = {s["name"] for s in schemas}
    assert visible_names == core_names, (
        "schemas_for_adapter(enabled_extended=set()) must show exactly the "
        "instance-tiered core set on the live boot registry"
    )
    assert "vault_ingest" not in visible_names
