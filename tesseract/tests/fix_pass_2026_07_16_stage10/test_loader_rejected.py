"""Stage 10 — `agents/rejected/` is an archive, never a load path.

`_find_agent_path` and `list_agents` scan one level of subdirectories into
the ACTIVE set (grouped families like `audits/`). Without an explicit skip,
an operator-rejected agent parked in `agents/rejected/` would remain
loadable — defeating the rejection. These tests pin the skip plus the
`list_rejected_agents` helper agent_create uses for re-proposal dedup.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.agents.loader import list_agents, list_rejected_agents, load_agent

_AGENT_MD = """---
name: {name}
version: "0.1"
model_role: agents_default
description: test fixture
---

## Role

John Doe specialist fixture.
"""


def _seed(agents_dir: Path, rel: str, name: str) -> Path:
    path = agents_dir / rel / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_AGENT_MD.format(name=name), encoding="utf-8")
    return path


def test_rejected_agent_not_listed_and_not_loadable(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    _seed(agents_dir, ".", "active-one")
    _seed(agents_dir, "rejected", "bad-idea")

    names = list_agents(agents_dir)
    assert "active-one" in names
    assert "bad-idea" not in names

    with pytest.raises(FileNotFoundError):
        load_agent("bad-idea", agents_dir=agents_dir)


def test_rejected_invisible_even_with_include_pending(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    _seed(agents_dir, "rejected", "bad-idea")
    assert "bad-idea" not in list_agents(agents_dir, include_pending=True)
    with pytest.raises(FileNotFoundError):
        load_agent("bad-idea", agents_dir=agents_dir, include_pending=True)


def test_list_rejected_agents(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    _seed(agents_dir, "rejected", "bad-idea")
    _seed(agents_dir, "rejected", "worse-idea")
    (agents_dir / "rejected" / "INDEX.md").write_text("| x |", encoding="utf-8")
    assert list_rejected_agents(agents_dir) == ["bad-idea", "worse-idea"]


def test_list_rejected_agents_missing_dir(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    assert list_rejected_agents(agents_dir) == []
