"""AU-4 S2 — agenda first-boot bootstrap idempotency."""

from __future__ import annotations

from pathlib import Path


def test_bootstrap_writes_seed_first_time(isolated_home: Path) -> None:
    from tesseract.orchestrator.autonomy import bootstrap_agenda
    from tesseract.orchestrator.autonomy.paths import (
        agenda_archive_dir,
        agenda_root,
    )

    item = bootstrap_agenda()
    assert item is not None
    # Seeded then driven to DONE → lives in archive, not active.
    assert any(agenda_archive_dir().rglob(f"{item.id}.json"))
    # Sentinel landed.
    assert (agenda_root() / ".bootstrap").exists()


def test_bootstrap_idempotent(isolated_home: Path) -> None:
    from tesseract.orchestrator.autonomy import bootstrap_agenda

    first = bootstrap_agenda()
    assert first is not None
    second = bootstrap_agenda()
    assert second is None  # sentinel short-circuits the re-seed
