"""Smoke-test that the from-scratch authoring agents (design-writer,
research-brief) load cleanly from `tesseract/agents/`.

Each agent's frontmatter must parse, the `## Role` section must be
non-empty, and `model_role` must be a recognized role name. Catches
broken YAML / typos in the agent .md files at CI time before a live
invoke discovers the bug at runtime."""

from __future__ import annotations

import pytest

from tesseract.agents.loader import list_agents, load_agent

NEW_AGENTS: list[tuple[str, list[str] | None, list[str]]] = [
    ("design-writer", None, ["Document Structure"]),
    ("research-brief", None, ["How to research", "Document Structure"]),
]


@pytest.mark.parametrize("slug,expected_tools,extra_sections", NEW_AGENTS)
def test_new_agent_loads_with_expected_shape(
    slug: str,
    expected_tools: list[str] | None,
    extra_sections: list[str],
) -> None:
    agent = load_agent(slug)

    assert agent.name == slug
    assert agent.model_role == "agents_default"
    assert agent.description.strip(), "description must be non-empty"
    assert agent.get_section("Role"), "## Role section must be non-empty"
    for section in extra_sections:
        assert agent.get_section(section), f"## {section} section must be non-empty"
    assert agent.tools == expected_tools


def test_new_agents_listed_under_registered_slugs() -> None:
    slugs = set(list_agents())
    for name, _, _ in NEW_AGENTS:
        assert name in slugs, f"{name} must show up in list_agents() so chat + agents can reference it"
