"""Per-agent model selection: the `model_role` frontmatter field accepts
either a role name from roles.yaml (legacy) or a provider-model reference
like `api.openai.gpt54_nano` (new) so an operator can pin the cheapest
viable fit per agent without standing up a new role.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.agents.loader import load_agent
from tesseract.kernel.tools.invoke_agent import _is_cli_role, _is_provider_ref


def _write_agent(tmp_path: Path, name: str, model_role: str) -> Path:
    body = (
        "---\n"
        f"name: {name}\n"
        f"model_role: {model_role}\n"
        "description: test fixture\n"
        "---\n"
        "## Role\n\nyou are a test agent.\n"
    )
    path = tmp_path / f"{name}.md"
    path.write_text(body, encoding="utf-8")
    return path


def test_agent_with_role_name_loads(tmp_path: Path) -> None:
    _write_agent(tmp_path, "alpha", "chat_brain")
    agent = load_agent("alpha", agents_dir=tmp_path)
    assert agent.model_role == "chat_brain"


def test_agent_with_provider_ref_loads(tmp_path: Path) -> None:
    _write_agent(tmp_path, "beta", "api.openai.gpt54_nano")
    agent = load_agent("beta", agents_dir=tmp_path)
    assert agent.model_role == "api.openai.gpt54_nano"
    assert _is_provider_ref(agent.model_role)


def test_provider_ref_detection_matches_three_part_form() -> None:
    assert _is_provider_ref("api.openai.gpt54_nano")
    assert _is_provider_ref("cli.claude.opus_47")
    assert _is_provider_ref("local.ollama.nomic_embed")
    assert not _is_provider_ref("chat_brain")
    assert not _is_provider_ref("api.openai")
    assert not _is_provider_ref("api.openai.gpt54_nano.extra")
    assert not _is_provider_ref("")


def test_cli_provider_ref_classified_as_cli_role() -> None:
    """`cli.<provider>.<model>` should be detected as a CLI subscription model
    so invoke_agent steers the operator to delegate_claude / delegate_codex."""
    assert _is_cli_role("cli.claude.opus_47") is True
    assert _is_cli_role("cli.codex.gpt55") is True
    assert _is_cli_role("api.openai.gpt54_nano") is False
    assert _is_cli_role("api.anthropic.haiku_45") is False
    # Legacy forms still recognized.
    assert _is_cli_role("claude_cli") is True
    assert _is_cli_role("cli_codex") is True
