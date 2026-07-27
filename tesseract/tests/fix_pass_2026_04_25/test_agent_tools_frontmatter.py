"""P15-E: Agent frontmatter `tools:` list flows through invoke_agent.

Previously `_tools_override_from_frontmatter` returned None
unconditionally, so every sub-agent got the read-only
DEFAULT_TOOL_SUBSET regardless of what the agent card declared. With
the frontmatter field wired, any agent can declare its own write-side
tools — gated by the parent registry + permission policy.
"""

from __future__ import annotations

import pytest

from tesseract.agents.loader import AgentDefinition, load_agent
from tesseract.kernel.tools.invoke_agent import (
    DEFAULT_TOOL_SUBSET,
    _tools_override_from_frontmatter,
)


def test_override_returns_frozenset_when_tools_declared():
    agent = AgentDefinition(
        name="dummy",
        model_role="chat_brain",
        tools=["file_read", "memory_search"],
    )
    override = _tools_override_from_frontmatter(agent)
    assert override == frozenset({"file_read", "memory_search"})


def test_override_returns_none_when_tools_omitted():
    agent = AgentDefinition(name="dummy", model_role="chat_brain", tools=None)
    override = _tools_override_from_frontmatter(agent)
    assert override is None


def test_override_respects_explicit_empty_list():
    """`tools: []` means "no tools" — sub-session is text-only."""
    agent = AgentDefinition(name="dummy", model_role="chat_brain", tools=[])
    override = _tools_override_from_frontmatter(agent)
    assert override == frozenset()
    # Empty override is meaningfully different from "use DEFAULT_TOOL_SUBSET".
    assert override is not None


def test_loader_rejects_non_list_tools_field(tmp_path):
    """Frontmatter `tools:` must be a list — anything else is a config bug."""
    bad = tmp_path / "agents"
    bad.mkdir()
    (bad / "broken.md").write_text(
        "---\n"
        "name: broken\n"
        "model_role: chat_brain\n"
        "tools: memory_search\n"  # string, not list
        "---\n"
        "## Identity\nbroken\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="tools.*list"):
        load_agent("broken", agents_dir=bad)


def test_default_tool_subset_unchanged():
    """If a regression strips the read-only fallback, every sub-agent
    suddenly gets ALL parent tools — that would be very bad. Pin it."""
    expected = {
        "file_read",
        "glob",
        "grep",
        "pdf_read",
        "memory_search",
        "vault_search",
        "vault_query",
        "context7_lookup",
        "web_search",
        "tavily_search",
        "tavily_extract",
    }
    assert set(DEFAULT_TOOL_SUBSET) == expected
