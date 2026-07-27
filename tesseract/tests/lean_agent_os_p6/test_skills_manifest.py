"""P6 Task 4/4b — skills pointer block in the assembled system prompt.

`assemble_system_prompt` (manifest mode) surfaces `workspace/skills/*/
SKILL.md` name+description via `brain/prompt.py::_build_skills_block`.
Uses the real prompt builder (not a stub) per the task brief.
"""

from __future__ import annotations

import logging
from pathlib import Path

from tesseract.brain import prompt as prompt_module


def _write_skill(workspace_dir: Path, dirname: str, name: str, description: str) -> None:
    folder = workspace_dir / "skills" / dirname
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nBody instructions.\n",
        encoding="utf-8",
    )


def _build(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    memory_store_dir = tmp_path / "memory-store"
    return workspace_dir, memory_store_dir


def test_valid_skill_appears_in_manifest(tmp_path: Path, monkeypatch) -> None:
    workspace_dir, memory_store_dir = _build(tmp_path, monkeypatch)
    _write_skill(workspace_dir, "daily-brief", "daily-brief", "Formats the morning brief consistently.")

    rendered = prompt_module.assemble_system_prompt(
        workspace_dir=workspace_dir, memory_store_dir=memory_store_dir,
    )
    assert "# Skills" in rendered
    assert "daily-brief" in rendered
    assert "Formats the morning brief consistently." in rendered


def test_script_bearing_skill_appears_but_script_contents_do_not(tmp_path: Path, monkeypatch) -> None:
    workspace_dir, memory_store_dir = _build(tmp_path, monkeypatch)
    _write_skill(workspace_dir, "with-script", "with-script", "Runs a bundled helper.")
    scripts_dir = workspace_dir / "skills" / "with-script" / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "helper.py").write_text("print('SECRET_SCRIPT_MARKER_XYZ')\n", encoding="utf-8")

    rendered = prompt_module.assemble_system_prompt(
        workspace_dir=workspace_dir, memory_store_dir=memory_store_dir,
    )
    assert "with-script" in rendered
    assert "Runs a bundled helper." in rendered
    assert "SECRET_SCRIPT_MARKER_XYZ" not in rendered


def test_malformed_frontmatter_skipped_prompt_still_builds_other_skills_still_listed(
    tmp_path: Path, monkeypatch, caplog,
) -> None:
    workspace_dir, memory_store_dir = _build(tmp_path, monkeypatch)
    _write_skill(workspace_dir, "good-skill", "good-skill", "A working skill.")
    broken = workspace_dir / "skills" / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("no frontmatter here\n", encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="tesseract.brain.skills"):
        rendered = prompt_module.assemble_system_prompt(
            workspace_dir=workspace_dir, memory_store_dir=memory_store_dir,
        )
    assert "good-skill" in rendered
    # The malformed skill must not be LISTED — assert its manifest pointer is
    # absent (a bare `"broken" not in rendered` false-positives on unrelated
    # prompt text, e.g. rule-16's "leave the broken embed on screen").
    assert "skills/broken/SKILL.md" not in rendered
    assert any("missing YAML frontmatter" in r.message for r in caplog.records)


def test_missing_skills_dir_omits_section_no_error(tmp_path: Path, monkeypatch) -> None:
    workspace_dir, memory_store_dir = _build(tmp_path, monkeypatch)

    rendered = prompt_module.assemble_system_prompt(
        workspace_dir=workspace_dir, memory_store_dir=memory_store_dir,
    )
    assert "# Skills" not in rendered
