"""P6 Task 4/4b — markdown skills loader (`tesseract/brain/skills.py`).

Design: `Docs/Plan/lean-agent-os/phase-6-alive.md` Task 4/4b +
`p6-p7-alive-scout-design.md` §Decisions(3)/§P6 amendment. Skill folders
(`workspace/skills/<name>/SKILL.md`) are parsed for name/description
frontmatter only; malformed/unreadable folders are SKIPPED (never a
prompt-build failure) with an ERROR-level log so the skip reaches the
Mirror pulse feed (`log_forwarder.py::MirrorLogHandler` forwards ERROR+).
"""

from __future__ import annotations

import logging
from pathlib import Path

from tesseract.brain.skills import SKILL_MD_MAX_BYTES, load_skills


def _write_skill(root: Path, dirname: str, name: str, description: str, body: str = "Do the thing.") -> Path:
    folder = root / dirname
    folder.mkdir(parents=True, exist_ok=True)
    skill_md = folder / "SKILL.md"
    skill_md.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )
    return skill_md


def test_missing_skills_dir_returns_empty(tmp_path: Path) -> None:
    assert load_skills(tmp_path / "skills") == []


def test_empty_skills_dir_returns_empty(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    assert load_skills(skills_dir) == []


def test_valid_skill_loaded(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "daily-brief", "daily-brief", "Formats the morning brief consistently.")
    entries = load_skills(skills_dir)
    assert len(entries) == 1
    assert entries[0].name == "daily-brief"
    assert entries[0].description == "Formats the morning brief consistently."
    assert entries[0].dirname == "daily-brief"


def test_script_bearing_skill_loaded_same_as_prose_only(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "with-script", "with-script", "Runs a bundled helper.")
    scripts_dir = skills_dir / "with-script" / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "helper.py").write_text("print('SECRET_SCRIPT_MARKER')\n", encoding="utf-8")

    entries = load_skills(skills_dir)
    assert len(entries) == 1
    assert entries[0].name == "with-script"
    assert entries[0].description == "Runs a bundled helper."


def test_folder_without_skill_md_silently_ignored(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "not-a-skill").mkdir()
    assert load_skills(skills_dir) == []


def test_missing_frontmatter_skipped_with_error_log(tmp_path: Path, caplog) -> None:
    skills_dir = tmp_path / "skills"
    folder = skills_dir / "broken"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("no frontmatter here, just prose\n", encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="tesseract.brain.skills"):
        entries = load_skills(skills_dir)
    assert entries == []
    assert any("missing YAML frontmatter" in r.message for r in caplog.records)


def test_invalid_yaml_frontmatter_skipped_with_error_log(tmp_path: Path, caplog) -> None:
    skills_dir = tmp_path / "skills"
    folder = skills_dir / "bad-yaml"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: [unclosed\n---\nbody\n", encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="tesseract.brain.skills"):
        entries = load_skills(skills_dir)
    assert entries == []
    assert any("frontmatter YAML invalid" in r.message for r in caplog.records)


def test_missing_name_or_description_skipped_with_error_log(tmp_path: Path, caplog) -> None:
    skills_dir = tmp_path / "skills"
    folder = skills_dir / "no-description"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: no-description\n---\nbody\n", encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="tesseract.brain.skills"):
        entries = load_skills(skills_dir)
    assert entries == []
    assert any("missing required name/description" in r.message for r in caplog.records)


def test_non_utf8_skill_md_skipped_with_error_log_never_raises(tmp_path: Path, caplog) -> None:
    """Windows-plausible: a SKILL.md saved with non-UTF-8 bytes must not
    raise UnicodeDecodeError past `load_skills` — skip + pulse-warning,
    same as any other malformed skill."""
    skills_dir = tmp_path / "skills"
    folder = skills_dir / "bad-encoding"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_bytes(b"---\nname: x\n---\n\xff\xfe garbage")

    with caplog.at_level(logging.ERROR, logger="tesseract.brain.skills"):
        entries = load_skills(skills_dir)
    assert entries == []
    assert any("unreadable" in r.message for r in caplog.records)


def test_non_utf8_skill_md_does_not_block_valid_siblings(tmp_path: Path, caplog) -> None:
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "good-skill", "good-skill", "A working skill.")
    folder = skills_dir / "bad-encoding"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_bytes(b"---\nname: x\n---\n\xff\xfe garbage")

    with caplog.at_level(logging.ERROR, logger="tesseract.brain.skills"):
        entries = load_skills(skills_dir)
    names = [e.name for e in entries]
    assert names == ["good-skill"]


def test_malformed_skill_does_not_block_valid_siblings(tmp_path: Path, caplog) -> None:
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "good-skill", "good-skill", "A working skill.")
    folder = skills_dir / "broken"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("no frontmatter\n", encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="tesseract.brain.skills"):
        entries = load_skills(skills_dir)
    names = [e.name for e in entries]
    assert names == ["good-skill"]


def test_oversized_skill_md_skipped_with_error_log(tmp_path: Path, caplog) -> None:
    skills_dir = tmp_path / "skills"
    folder = skills_dir / "huge"
    folder.mkdir(parents=True)
    body = "x" * (SKILL_MD_MAX_BYTES + 1)
    (folder / "SKILL.md").write_text(
        f"---\nname: huge\ndescription: too big\n---\n{body}\n", encoding="utf-8",
    )

    with caplog.at_level(logging.ERROR, logger="tesseract.brain.skills"):
        entries = load_skills(skills_dir)
    assert entries == []
    assert any("exceeds" in r.message for r in caplog.records)


def test_oversized_skill_md_does_not_block_valid_siblings(tmp_path: Path, caplog) -> None:
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "good-skill", "good-skill", "A working skill.")
    folder = skills_dir / "huge"
    folder.mkdir(parents=True)
    body = "x" * (SKILL_MD_MAX_BYTES + 1)
    (folder / "SKILL.md").write_text(
        f"---\nname: huge\ndescription: too big\n---\n{body}\n", encoding="utf-8",
    )

    with caplog.at_level(logging.ERROR, logger="tesseract.brain.skills"):
        entries = load_skills(skills_dir)
    names = [e.name for e in entries]
    assert names == ["good-skill"]


def test_skill_md_at_exact_cap_boundary_loads(tmp_path: Path) -> None:
    """A SKILL.md whose total byte size is exactly at the cap must still
    load — the guard only rejects strictly-over-cap files."""
    skills_dir = tmp_path / "skills"
    folder = skills_dir / "boundary"
    folder.mkdir(parents=True)
    skill_md = folder / "SKILL.md"
    header = "---\nname: boundary\ndescription: right at the cap\n---\n"
    padding_len = SKILL_MD_MAX_BYTES - len(header.encode("utf-8"))
    skill_md.write_bytes(header.encode("utf-8") + b"x" * padding_len)
    assert skill_md.stat().st_size == SKILL_MD_MAX_BYTES

    entries = load_skills(skills_dir)
    assert len(entries) == 1
    assert entries[0].name == "boundary"


def test_unreadable_skills_dir_returns_empty_never_raises(tmp_path: Path, monkeypatch) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    def _boom(self):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "iterdir", _boom)
    assert load_skills(skills_dir) == []
