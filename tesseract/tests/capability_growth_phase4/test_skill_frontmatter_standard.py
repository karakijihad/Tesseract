"""Phase 4 — SKILL.md frontmatter aligned to the Agent Skills standard.

name/description stay required; version/license/allowed-tools/metadata are
optional passthrough. A skill missing the optional fields still loads (back-
compat), and the loader skips the pending/rejected quarantine dirs.
"""

from __future__ import annotations

from pathlib import Path

from tesseract.brain.skills import load_skills


def _write(skills: Path, name: str, body: str) -> None:
    d = skills / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")


def test_optional_standard_fields_parsed(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write(skills, "full", (
        "---\n"
        "name: full\n"
        "description: full standard skill for when X\n"
        "version: '1.2'\n"
        "license: MIT\n"
        "allowed-tools:\n  - file_read\n  - grep\n"
        "metadata:\n  author: Jane Doe\n"
        "---\n\nbody\n"
    ))
    entries = {s.name: s for s in load_skills(skills)}
    s = entries["full"]
    assert s.version == "1.2"
    assert s.license == "MIT"
    assert s.allowed_tools == ("file_read", "grep")
    assert s.metadata == {"author": "Jane Doe"}


def test_allowed_tools_accepts_string_form(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write(skills, "strform", (
        "---\nname: strform\ndescription: str allowed-tools for Y\n"
        "allowed-tools: Read Grep\n---\n\nbody\n"
    ))
    s = {x.name: x for x in load_skills(skills)}["strform"]
    assert s.allowed_tools == ("Read", "Grep")


def test_minimal_skill_still_loads(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write(skills, "minimal", "---\nname: minimal\ndescription: bare skill for Z\n---\n\nbody\n")
    entries = load_skills(skills)
    assert [e.name for e in entries] == ["minimal"]
    assert entries[0].version == "" and entries[0].allowed_tools == ()


def test_loader_skips_quarantine_dirs(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write(skills, "active", "---\nname: active\ndescription: active one for W\n---\n\nbody\n")
    # pending/rejected each hold a skill folder — must NOT be surfaced.
    _write(skills / "pending", "draft", "---\nname: draft\ndescription: draft for V\n---\n\nb\n")
    _write(skills / "rejected", "nope", "---\nname: nope\ndescription: nope for U\n---\n\nb\n")
    names = [s.name for s in load_skills(skills)]
    assert names == ["active"]
