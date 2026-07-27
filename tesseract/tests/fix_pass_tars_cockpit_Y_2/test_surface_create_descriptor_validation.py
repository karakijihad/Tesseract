"""Descriptor validation + create-verb behavior."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tesseract.orchestrator.surfaces.descriptor import (
    SCHEMA_VERSION,
    SurfaceDescriptor,
)
from tesseract.orchestrator.surfaces.store import get_surface_store


def _minimal_kwargs(**over):
    base = dict(
        id="folder-tars-abc123",
        type="folder",
        view="tars",
        position={"x": 10, "y": 20},
        size={"w": 600, "h": 400},
        created_at_utc="2026-06-03T00:00:00+00:00",
        updated_at_utc="2026-06-03T00:00:00+00:00",
    )
    base.update(over)
    return base


def test_minimal_descriptor_validates_with_defaults():
    d = SurfaceDescriptor(**_minimal_kwargs())
    assert d.schema_version == SCHEMA_VERSION
    assert d.mode == "embedded"
    assert d.z == 0
    assert d.locked is False
    assert d.props == {}
    assert d.bound_session is None


@pytest.mark.parametrize("missing", ["id", "type", "view", "position", "size"])
def test_required_fields_enforced(missing):
    kwargs = _minimal_kwargs()
    kwargs.pop(missing)
    with pytest.raises(ValidationError):
        SurfaceDescriptor(**kwargs)


def test_bad_mode_rejected():
    with pytest.raises(ValidationError):
        SurfaceDescriptor(**_minimal_kwargs(mode="floating"))


def test_create_returns_id_and_lists(isolated_home):
    store = get_surface_store()
    sid = store.create(
        type="folder", view="tars", props={"root": "/tmp/x"}, title="X"
    )
    assert sid.startswith("folder-tars-")
    rows = store.list_for_view("tars")
    assert len(rows) == 1
    assert rows[0]["id"] == sid
    assert rows[0]["props"]["root"] == "/tmp/x"
    # First surface gets z=1 (next_z over an empty view).
    assert rows[0]["z"] == 1


def test_external_mode_create_records_no_card(isolated_home):
    store = get_surface_store()
    sid = store.create(type="url", view="tars", mode="external", props={"url": "x"})
    assert sid  # descriptor minted + event published
    assert store.list_for_view("tars") == []  # but no canvas card


def test_focus_raises_z_to_top(isolated_home):
    store = get_surface_store()
    a = store.create(type="folder", view="tars")
    b = store.create(type="file", view="tars")
    store.focus(a)
    rows = {r["id"]: r["z"] for r in store.list_for_view("tars")}
    assert rows[a] > rows[b]
