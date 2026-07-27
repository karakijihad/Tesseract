"""Persistence round-trip + layer-disjointness + operator-event geometry."""

from __future__ import annotations

import json

from tesseract.orchestrator.surfaces.persistence import (
    canvas_state_dir,
    write_view_blob,
)
from tesseract.orchestrator.surfaces.store import (
    SurfaceStore,
    get_surface_store,
)


def _read_file(view: str) -> dict:
    return json.loads((canvas_state_dir() / f"{view}.json").read_text("utf-8"))


def test_create_persists_and_survives_restart(isolated_home):
    sid = get_surface_store().create(type="folder", view="tars", props={"root": "/r"})
    # A fresh store (== brain restart) hydrates the same surface from disk.
    fresh = SurfaceStore()
    rows = fresh.list_for_view("tars")
    assert [r["id"] for r in rows] == [sid]


def test_backend_write_preserves_frontend_tldraw_snapshot(isolated_home):
    # Frontend saved its tldraw snapshot first (Y-1 round-trip).
    write_view_blob(
        "tars",
        {
            "schema_version": 1,
            "view": "tars",
            "viewport": {"x": 5, "y": 6, "zoom": 1.5},
            "surfaces": [],
            "tldraw_snapshot": {"store": {"shape:1": {}}},
        },
    )
    get_surface_store().create(type="file", view="tars")
    blob = _read_file("tars")
    assert blob["tldraw_snapshot"] == {"store": {"shape:1": {}}}
    assert blob["viewport"] == {"x": 5, "y": 6, "zoom": 1.5}
    assert len(blob["surfaces"]) == 1


def test_operator_move_event_persists_new_position(isolated_home):
    store = get_surface_store()
    sid = store.create(type="folder", view="tars", position={"x": 0, "y": 0})
    ok = store.apply_event(
        view="tars",
        surface_id=sid,
        event="moved",
        detail={"position": {"x": 333, "y": 444}},
    )
    assert ok
    blob = _read_file("tars")
    assert blob["surfaces"][0]["position"] == {"x": 333.0, "y": 444.0}


def test_operator_close_event_removes_surface(isolated_home):
    store = get_surface_store()
    sid = store.create(type="folder", view="tars")
    assert store.apply_event(view="tars", surface_id=sid, event="closed", detail={})
    assert store.list_for_view("tars") == []


def test_mutate_after_restart_hydrates_via_disk_fallback(isolated_home):
    # Surface created + persisted in one process lifetime...
    sid = get_surface_store().create(type="folder", view="tars", title="orig")
    # ...then a fresh store (brain restart) mutates it WITHOUT first touching
    # the view via create/list — _get must hydrate from disk to find it.
    fresh = SurfaceStore()
    assert fresh.update(sid, title="renamed") is not None
    assert fresh.get(sid)["title"] == "renamed"
    assert fresh.close(sid) is True
    assert fresh.get(sid) is None


def test_operator_close_event_does_not_republish(isolated_home):
    from tesseract.orchestrator.background_event_bus import get_background_bus

    store = get_surface_store()
    sid = store.create(type="folder", view="tars")
    bus = get_background_bus()
    _replay, queue = bus.subscribe()  # subscribe AFTER create
    assert store.apply_event(view="tars", surface_id=sid, event="closed", detail={})
    # Operator-origin close must not echo a surface_closed back to clients.
    assert queue.empty()


def test_create_publishes_surface_created_event(isolated_home):
    from tesseract.orchestrator.background_event_bus import get_background_bus

    bus = get_background_bus()
    replay, queue = bus.subscribe()
    sid = get_surface_store().create(type="folder", view="tars")
    event = queue.get_nowait()
    assert event.type == "surface_created"
    assert event.data["channel"] == "surface"
    assert event.data["view"] == "tars"
    assert event.data["data"]["id"] == sid
