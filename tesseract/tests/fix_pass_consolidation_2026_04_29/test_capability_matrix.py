"""Phase 18.5 W7-A — `Docs/Logs/CAPABILITIES.md` must stay in sync with
the live tool registry. The script renders deterministically and the
`--check` flag is the CI gate.
"""

from __future__ import annotations

from pathlib import Path

from tesseract.scripts.generate_capability_matrix import OUTPUT_PATH, render_matrix


def test_render_matrix_is_deterministic() -> None:
    """Running the renderer twice in the same process must produce
    identical output — sorted tool order, no timestamps, no random
    fields."""
    a = render_matrix()
    b = render_matrix()
    assert a == b


def test_capabilities_md_matches_registry() -> None:
    """The committed `Docs/Logs/CAPABILITIES.md` must equal what the
    live registry renders. CI runs the script with --check; this test
    is the local-dev guard."""
    assert OUTPUT_PATH.exists(), (
        f"{OUTPUT_PATH} missing — run `python -m tesseract.scripts.generate_capability_matrix`"
    )
    rendered = render_matrix().strip()
    on_disk = OUTPUT_PATH.read_text(encoding="utf-8").strip()
    assert rendered == on_disk, (
        f"{OUTPUT_PATH} drifted from the live registry. "
        "Run `python -m tesseract.scripts.generate_capability_matrix` to refresh."
    )


def test_matrix_lists_phase_18_tools() -> None:
    """schedule_create + schedule_remove (Phase 18 Task B) must be in
    the matrix. Guards against accidental registry shrinkage."""
    rendered = render_matrix()
    assert "`schedule_create`" in rendered
    assert "`schedule_remove`" in rendered
    assert "`set_voice`" in rendered  # Phase 16 voice
