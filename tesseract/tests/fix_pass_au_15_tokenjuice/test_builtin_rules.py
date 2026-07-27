"""AU-15: builtin rules parse and classify the tools they claim."""
from __future__ import annotations

from tesseract.kernel.tokenjuice import BUILTIN_RULES_DIR, classify, load_rules


def test_all_builtin_rules_parse():
    rules = load_rules(BUILTIN_RULES_DIR)
    assert len(rules) >= 7


def test_builtin_bash_git_status_matches():
    rules = load_rules(BUILTIN_RULES_DIR)
    hit = classify(rules, "bash_exec", {"command": "git status -s"})
    assert hit is not None
    assert hit.id == "bash_exec.git_status"


def test_builtin_bash_unrelated_command_no_match():
    rules = load_rules(BUILTIN_RULES_DIR)
    assert classify(rules, "bash_exec", {"command": "echo hello"}) is None


def test_builtin_vault_search_matches_any_args():
    rules = load_rules(BUILTIN_RULES_DIR)
    hit = classify(rules, "vault_search", {"query": "anything"})
    assert hit is not None
    assert hit.id == "vault_search.default"


def test_builtin_delegate_claude_matches():
    rules = load_rules(BUILTIN_RULES_DIR)
    hit = classify(rules, "delegate_claude", {"prompt": "x"})
    assert hit is not None
    assert hit.family == "delegate"


def test_builtin_tavily_extract_matches_tavily_rule():
    rules = load_rules(BUILTIN_RULES_DIR)
    # tavily_search rule covers tavily_extract + web_search per its tool_names list
    hit = classify(rules, "tavily_extract", {})
    assert hit is not None
    assert hit.id == "tavily_search.default"


def test_builtin_rules_have_unique_ids():
    rules = load_rules(BUILTIN_RULES_DIR)
    ids = [r.id for r in rules]
    assert len(ids) == len(set(ids))
