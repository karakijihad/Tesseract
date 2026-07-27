"""Y-3 — Terminal canvas baseline layout.

The Terminal view migrated to a canvas: the real multi-tab terminal renders
as a single ``terminal-host`` Surface Protocol card seeded from
orchestrator/surfaces/defaults/terminal.json. Multi-tab + split-pane behaviour
is preserved inside that host card (frontend concern; vitest/Playwright);
here we assert the single host card seeds (a single mount point keeps the PTY
bootstrap firing exactly once).
"""

from __future__ import annotations

from pathlib import Path

from tesseract.orchestrator.surfaces.store import get_surface_store


def test_terminal_baseline_seeds_single_host(isolated_home: Path):
    cards = get_surface_store().list_for_view("terminal")
    hosts = [c for c in cards if c["type"] == "terminal-host"]
    assert len(hosts) == 1


def test_terminal_host_embedded_not_external(isolated_home: Path):
    host = next(
        c for c in get_surface_store().list_for_view("terminal") if c["type"] == "terminal-host"
    )
    # Embedded → renders as a canvas card (not an OS-native external surface).
    assert host.get("mode", "embedded") == "embedded"
