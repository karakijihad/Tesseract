"""SU-3.5: audit sub-agents — structural tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_TESSERACT_PKG = Path(__file__).resolve().parents[2]
_AGENTS_DIR = _TESSERACT_PKG / "agents"
_AUDITS_DIR = _AGENTS_DIR / "audits"
_INDEX_PATH = _AGENTS_DIR / "INDEX.md"

_AGENT_FILES = {
    "code-auditor": _AUDITS_DIR / "code-auditor.md",
    "repo-auditor": _AUDITS_DIR / "repo-auditor.md",
    "audit-verifier": _AUDITS_DIR / "audit-verifier.md",
    "code-fixer": _AUDITS_DIR / "code-fixer.md",
}

_AUDIT_PRODUCING_AGENTS = {"code-auditor", "repo-auditor", "audit-verifier"}
_SEVERITY_TOKENS = {"Critical", "Major", "Minor", "Informational"}

_EXPECTED_TOOL = {
    "code-auditor": "delegate_codex_exec",
    "repo-auditor": "delegate_codex_exec",
    "audit-verifier": "delegate_codex_exec",
    # `code-fixer` routes through delegate_tars_controller since the
    # 2026-05-25 daemon-delegate removal (see Docs/Sessions/2026-05-25.md).
    "code-fixer": "delegate_tars_controller",
}


def _parse_frontmatter(text: str) -> dict:
    """Extract YAML frontmatter between the first two ``---`` fences."""
    parts = text.split("---")
    if len(parts) < 3:
        return {}
    return yaml.safe_load(parts[1]) or {}


# Pre-parsed frontmatter for all agent files — avoids repeated read+parse per test.
_FRONTMATTER: dict[str, dict] = {
    name: _parse_frontmatter(path.read_text(encoding="utf-8"))
    for name, path in _AGENT_FILES.items()
    if path.exists()
}


class TestFrontmatter:
    def test_all_files_exist(self):
        for name, path in _AGENT_FILES.items():
            assert path.exists(), f"{name}: file not found at {path}"

    def test_required_fields_present(self):
        required = {"name", "description", "underlying_tool", "default_posture"}
        for name, fm in _FRONTMATTER.items():
            missing = required - set(fm.keys())
            assert not missing, f"{name}: frontmatter missing fields {missing}"

    def test_model_role_declared(self):
        for name, fm in _FRONTMATTER.items():
            assert fm.get("model_role"), f"{name}: frontmatter must declare model_role"

    def test_name_field_matches_stem(self):
        for name, fm in _FRONTMATTER.items():
            assert fm.get("name") == name, (
                f"{name}: frontmatter `name` ({fm.get('name')!r}) does not match file stem"
            )


class TestIndexEntries:
    def test_all_agents_listed_in_index(self):
        index_text = _INDEX_PATH.read_text(encoding="utf-8")
        for name in _AGENT_FILES:
            assert name in index_text, f"INDEX.md does not mention agent {name!r}"


class TestUnderlyingTool:
    def test_underlying_tool_values(self):
        for name, fm in _FRONTMATTER.items():
            tool = fm.get("underlying_tool")
            expected = _EXPECTED_TOOL[name]
            assert tool == expected, (
                f"{name}: underlying_tool is {tool!r}, expected {expected!r}"
            )


class TestSeverityGrammar:
    def test_all_four_severity_tokens_present(self):
        for name in _AUDIT_PRODUCING_AGENTS:
            text = _AGENT_FILES[name].read_text(encoding="utf-8")
            missing = _SEVERITY_TOKENS - set(text.split())
            # Fallback: split may fragment tokens adjacent to punctuation.
            still_missing = {t for t in missing if t not in text}
            assert not still_missing, (
                f"{name}: body missing severity token(s): {still_missing}"
            )


# ── test 5: chat-brain prompt includes audit-loop routing ──────────────────

class TestPromptRouting:
    def test_audit_loop_directive_constant_exists(self):
        import tesseract.brain.prompt as p
        assert hasattr(p, "_AUDIT_LOOP_DIRECTIVE_TEXT"), (
            "tesseract.brain.prompt is missing _AUDIT_LOOP_DIRECTIVE_TEXT constant"
        )

    def test_audit_loop_routing_text_content(self):
        import tesseract.brain.prompt as p
        text = p._AUDIT_LOOP_DIRECTIVE_TEXT
        assert "Audit-loop routing" in text
        assert "code-auditor" in text
        assert "code-fixer" in text
        assert "audit-verifier" in text
        assert "repo-auditor" in text

    def test_audit_loop_directive_appended_to_sections(self, tmp_path, monkeypatch):
        """assemble_system_prompt includes the audit-loop routing block."""
        import tesseract.brain.prompt as p
        from unittest.mock import patch
        from datetime import datetime, timezone, timedelta

        # Minimal workspace — just needs to not raise.
        monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
        monkeypatch.setattr(p, "_IDENTITY_CONFIG_PATH", tmp_path / "nope.yaml")

        LOCAL = timezone(timedelta(hours=1))
        fixed = datetime(2026, 5, 20, 14, 0, tzinfo=LOCAL)
        with patch.object(p, "_now_local", return_value=fixed):
            prompt = p.assemble_system_prompt(
                workspace_dir=tmp_path / "workspace",
                memory_store_dir=tmp_path / "memory-store",
            )
        assert "Audit-loop routing" in prompt


# ── test 6: loader can find audit sub-agents by name ───────────────────────

class TestLoaderDiscovery:
    def test_load_agent_finds_subdirectory_agents(self):
        from tesseract.agents.loader import load_agent
        for name in _AGENT_FILES:
            defn = load_agent(name, agents_dir=_AGENTS_DIR)
            assert defn.name == name, f"load_agent returned wrong name for {name!r}"

    def test_list_agents_includes_subdirectory_agents(self):
        from tesseract.agents.loader import list_agents
        names = list_agents(agents_dir=_AGENTS_DIR)
        for name in _AGENT_FILES:
            assert name in names, f"list_agents did not return {name!r}"


# ── test 7: repo-auditor default_posture is ask ────────────────────────────

class TestRepoAuditorPosture:
    def test_repo_auditor_default_posture_is_ask(self):
        fm = _FRONTMATTER.get("repo-auditor", {})
        assert fm.get("default_posture") == "ask", (
            "repo-auditor: default_posture must be 'ask' (operator-initiated only)"
        )


# ── test 8: loader regression — top-level agents unaffected ───────────────

class TestLoaderRegression:
    def test_existing_top_level_agents_load(self):
        """Core top-level agents must still load after subdirectory scan change."""
        from tesseract.agents.loader import load_agent
        for name in ("observer", "research-brief", "vault-librarian"):
            defn = load_agent(name, agents_dir=_AGENTS_DIR)
            assert defn.name == name, f"load_agent returned wrong name for {name!r}"

    def test_list_agents_minimum_count(self):
        """list_agents must return the 4 audit agents + at least 20 top-level agents."""
        from tesseract.agents.loader import list_agents
        names = list_agents(agents_dir=_AGENTS_DIR)
        assert len(names) >= 24, (
            f"Expected >=24 agents, got {len(names)}: {sorted(names)}"
        )

    def test_provisional_subdir_excluded_from_loader(self, tmp_path):
        """Quarantined agents under provisional/ MUST NOT be discoverable."""
        from tesseract.agents.loader import load_agent, list_agents
        agents_dir = tmp_path / "agents"
        (agents_dir / "provisional").mkdir(parents=True)
        (agents_dir / "audits").mkdir()
        (agents_dir / "provisional" / "quarantined.md").write_text(
            '---\nname: quarantined\nversion: "0.1"\nmodel_role: agents_default\n'
            'description: should not be discovered\n---\nbody\n',
            encoding="utf-8",
        )
        (agents_dir / "audits" / "discoverable.md").write_text(
            '---\nname: discoverable\nversion: "0.1"\nmodel_role: agents_default\n'
            'description: should be discovered\n---\nbody\n',
            encoding="utf-8",
        )
        with pytest.raises(Exception):
            load_agent("quarantined", agents_dir=agents_dir)
        found = load_agent("discoverable", agents_dir=agents_dir)
        assert found.name == "discoverable"
        names = set(list_agents(agents_dir=agents_dir))
        assert "discoverable" in names
        assert "quarantined" not in names
