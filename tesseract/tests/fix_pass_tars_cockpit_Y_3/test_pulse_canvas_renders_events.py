"""Y-3 — Pulse canvas baseline layout.

The Pulse view migrated to a canvas: the event stream + filter chips render
as Surface Protocol cards seeded from the source-controlled baseline layout
(orchestrator/surfaces/defaults/pulse.json). These tests lock the seeding
contract the frontend renderers depend on. The actual event rendering is a
frontend concern (vitest + Playwright); here we assert the right cards seed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.orchestrator.surfaces.persistence import canvas_state_dir
from tesseract.orchestrator.surfaces.store import get_surface_store, reset_surface_store


def test_pulse_baseline_seeds_stream_and_filter_cards(isolated_home: Path):
    cards = get_surface_store().list_for_view("pulse")
    types = {c["type"] for c in cards}
    assert {"pulse-stream", "pulse-filters"} <= types


def test_pulse_baseline_seed_is_not_written_on_bare_list(isolated_home: Path):
    # A bare GET must not persist — seeding is in-memory only so a re-seed on
    # the next boot stays idempotent (stable ids) and the operator's first
    # interaction is what creates their file.
    get_surface_store().list_for_view("pulse")
    assert not (canvas_state_dir() / "pulse.json").exists()


def test_pulse_seed_ids_are_stable_across_reseed(isolated_home: Path):
    first = {c["id"] for c in get_surface_store().list_for_view("pulse")}
    reset_surface_store()  # simulate a brain restart
    second = {c["id"] for c in get_surface_store().list_for_view("pulse")}
    assert first == second == {"pulse-stream", "pulse-filters"}


def test_operator_interaction_persists_then_seed_is_respected(isolated_home: Path):
    store = get_surface_store()
    store.list_for_view("pulse")
    # Operator closes the filter card — apply_event persists the layout.
    assert store.apply_event(view="pulse", surface_id="pulse-filters", event="closed", detail={})
    assert (canvas_state_dir() / "pulse.json").exists()
    reset_surface_store()
    # The closed card stays closed — a file that exists is never re-seeded.
    types = {c["type"] for c in get_surface_store().list_for_view("pulse")}
    assert "pulse-filters" not in types
    assert "pulse-stream" in types
