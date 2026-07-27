"""surface_* kernel tool surface — posture, registration, end-to-end run."""

from __future__ import annotations

import pytest

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.surface_bind_session import SurfaceBindSessionTool
from tesseract.kernel.tools.surface_close import SurfaceCloseTool
from tesseract.kernel.tools.surface_create import SurfaceCreateTool, SurfaceCreateInput
from tesseract.kernel.tools.surface_focus import SurfaceFocusTool
from tesseract.kernel.tools.surface_highlight import SurfaceHighlightTool
from tesseract.kernel.tools.surface_list import SurfaceListTool, SurfaceListInput
from tesseract.kernel.tools.surface_update import SurfaceUpdateTool, SurfaceUpdateInput

ALL_TOOLS = [
    SurfaceCreateTool,
    SurfaceUpdateTool,
    SurfaceFocusTool,
    SurfaceCloseTool,
    SurfaceListTool,
    SurfaceHighlightTool,
    SurfaceBindSessionTool,
]


def test_all_surface_tools_auto_and_autonomous():
    for cls in ALL_TOOLS:
        t = cls()
        assert t.default_posture == "auto", t.name
        assert t.risk_class == "autonomous", t.name


def test_boot_registers_six_surface_tools():
    from tesseract.brain.boot import build_tool_registry

    registry, *_ = build_tool_registry()
    names = set(registry.names())
    assert {
        "surface_create",
        "surface_update",
        "surface_focus",
        "surface_close",
        "surface_list",
        "surface_highlight",
        "surface_bind_session",
    } <= names


def test_coherence_loop_tools_are_core_tier():
    """surface create/update/list/close + the read-only browser observe verbs
    are pinned core so they're in the manifest by default (not tool_search-gated)."""
    from tesseract.brain.boot import build_tool_registry

    registry, *_ = build_tool_registry()
    for name in (
        "surface_create", "surface_update", "surface_list", "surface_close",
        "browser_navigate", "browser_snapshot", "browser_screenshot",
    ):
        assert registry.tools[name].tier == "core", name


@pytest.mark.asyncio
async def test_create_then_update_then_close_round_trip(isolated_home):
    ctx = ToolContext()
    create = await SurfaceCreateTool().run(
        SurfaceCreateInput(type="folder", view="tars", props={"root": "/r"}), ctx
    )
    assert not create.is_error
    sid = create.metadata["surface_id"]

    upd = await SurfaceUpdateTool().run(
        SurfaceUpdateInput(surface_id=sid, title="renamed"), ctx
    )
    assert not upd.is_error

    from tesseract.orchestrator.surfaces.store import get_surface_store

    assert get_surface_store().get(sid)["title"] == "renamed"

    close = await SurfaceCloseTool().run(
        SurfaceCloseTool().input_schema(surface_id=sid), ctx
    )
    assert not close.is_error
    assert get_surface_store().get(sid) is None


@pytest.mark.asyncio
async def test_surface_list_reports_spawned_surfaces(isolated_home):
    ctx = ToolContext()
    empty = await SurfaceListTool().run(SurfaceListInput(view="tars"), ctx)
    assert not empty.is_error and empty.metadata["count"] == 0

    created = await SurfaceCreateTool().run(
        SurfaceCreateInput(type="webview", view="tars", props={"url": "https://x"}, title="vid"), ctx
    )
    sid = created.metadata["surface_id"]

    listed = await SurfaceListTool().run(SurfaceListInput(view="tars"), ctx)
    assert not listed.is_error
    assert listed.metadata["count"] == 1
    assert sid in listed.output
    assert "webview" in listed.output and "vid" in listed.output


@pytest.mark.asyncio
async def test_create_with_replaces_closes_old_surface(isolated_home):
    """A fallback surface_create replaces:<id> closes the dead card once the
    replacement exists — both never coexist, neither is left dangling."""
    from tesseract.orchestrator.surfaces.store import get_surface_store

    ctx = ToolContext()
    dead = await SurfaceCreateTool().run(
        SurfaceCreateInput(type="webview", view="tars", props={"url": "https://blocked"}), ctx
    )
    dead_sid = dead.metadata["surface_id"]

    replacement = await SurfaceCreateTool().run(
        SurfaceCreateInput(
            type="external-link", view="tars", props={"url": "https://blocked"},
            replaces=dead_sid,
        ),
        ctx,
    )
    assert not replacement.is_error
    new_sid = replacement.metadata["surface_id"]
    assert replacement.metadata["replaced"] == dead_sid
    assert get_surface_store().get(dead_sid) is None
    assert get_surface_store().get(new_sid) is not None


@pytest.mark.asyncio
async def test_create_with_unknown_replaces_still_succeeds(isolated_home):
    """An unknown/already-closed replaces id must not fail the create."""
    res = await SurfaceCreateTool().run(
        SurfaceCreateInput(type="markdown", view="tars", props={"text": "x"}, replaces="gone"),
        ToolContext(),
    )
    assert not res.is_error
    assert res.metadata["replaced"] is None


@pytest.mark.asyncio
async def test_update_unknown_id_is_error(isolated_home):
    res = await SurfaceUpdateTool().run(
        SurfaceUpdateInput(surface_id="nope", title="x"), ToolContext()
    )
    assert res.is_error
    assert "unknown surface_id" in res.output
