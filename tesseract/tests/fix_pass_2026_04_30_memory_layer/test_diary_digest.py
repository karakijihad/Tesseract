"""Phase 1 (b) — recent diary entries surface in the per-turn capsule.

The librarian distills diary into SOUL Growth on cron; this digest gives
TARS sight of its most recent reflections without waiting for the next
consolidation pass.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from tesseract.brain.prompt import (
    DIARY_DIGEST_CHAR_BUDGET,
    DIARY_DIGEST_DAYS,
    _build_diary_digest,
    assemble_system_prompt,
)


def _minimal_workspace(tmp_path: Path) -> tuple[Path, Path]:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("# Agents\n\nplaceholder\n", encoding="utf-8")
    store = tmp_path / "memory-store"
    store.mkdir()
    return ws, store


def test_digest_empty_when_no_diary_dir(tmp_path: Path) -> None:
    _, store = _minimal_workspace(tmp_path)
    assert _build_diary_digest(store) == ""


def test_digest_empty_when_diary_dir_has_no_files(tmp_path: Path) -> None:
    _, store = _minimal_workspace(tmp_path)
    (store / "diary").mkdir()
    assert _build_diary_digest(store) == ""


def test_digest_includes_recent_entries(tmp_path: Path) -> None:
    _, store = _minimal_workspace(tmp_path)
    diary = store / "diary"
    diary.mkdir()
    today = _dt.date.today()
    yesterday = today - _dt.timedelta(days=1)
    (diary / f"{today.isoformat()}.md").write_text(
        f"# Diary — {today.isoformat()}\n\n**12:00**  Today's reflection.\n",
        encoding="utf-8",
    )
    (diary / f"{yesterday.isoformat()}.md").write_text(
        f"# Diary — {yesterday.isoformat()}\n\n**18:00**  Yesterday's reflection.\n",
        encoding="utf-8",
    )

    digest = _build_diary_digest(store)
    assert "# Recent diary" in digest
    assert today.isoformat() in digest
    assert yesterday.isoformat() in digest
    # Newest first.
    assert digest.index(today.isoformat()) < digest.index(yesterday.isoformat())


def test_digest_skips_files_older_than_window(tmp_path: Path) -> None:
    _, store = _minimal_workspace(tmp_path)
    diary = store / "diary"
    diary.mkdir()
    old = _dt.date.today() - _dt.timedelta(days=DIARY_DIGEST_DAYS + 5)
    (diary / f"{old.isoformat()}.md").write_text("ancient entry", encoding="utf-8")

    digest = _build_diary_digest(store)
    assert digest == ""


def test_digest_respects_char_budget(tmp_path: Path) -> None:
    _, store = _minimal_workspace(tmp_path)
    diary = store / "diary"
    diary.mkdir()
    today = _dt.date.today()
    body = "x" * (DIARY_DIGEST_CHAR_BUDGET + 500)
    (diary / f"{today.isoformat()}.md").write_text(body, encoding="utf-8")

    digest = _build_diary_digest(store)
    assert "[truncated]" in digest
    # Cap is on the diary block, but section header + truncation marker add
    # a small fixed envelope. Allow generous slack — the rule is "bounded".
    assert len(digest) < DIARY_DIGEST_CHAR_BUDGET + 200


def test_assemble_system_prompt_inlines_diary(tmp_path: Path) -> None:
    ws, store = _minimal_workspace(tmp_path)
    diary = store / "diary"
    diary.mkdir()
    today = _dt.date.today()
    (diary / f"{today.isoformat()}.md").write_text(
        "**08:00**  First reflection of the day.", encoding="utf-8"
    )

    prompt = assemble_system_prompt(workspace_dir=ws, memory_store_dir=store, mode="manifest")
    assert "# Recent diary" in prompt
    assert "First reflection of the day" in prompt


def test_assemble_system_prompt_no_diary_no_section(tmp_path: Path) -> None:
    ws, store = _minimal_workspace(tmp_path)
    prompt = assemble_system_prompt(workspace_dir=ws, memory_store_dir=store, mode="manifest")
    assert "# Recent diary" not in prompt
