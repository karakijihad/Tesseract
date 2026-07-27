"""AU-15: Rule model + loader + classifier + three-layer merge."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tesseract.kernel.tokenjuice.rules import (
    Rule,
    _resolve_arg,
    classify,
    load_rules,
    load_rules_from_dir,
)


def _rule(idx: str, *, priority: int = 0, **match) -> Rule:
    return Rule.model_validate(
        {"id": idx, "family": "test", "priority": priority, "match": match}
    )


def test_load_rules_reads_single_object_file(tmp_path: Path):
    p = tmp_path / "r1.json"
    p.write_text(json.dumps({"id": "r1", "family": "t", "match": {"tool_names": ["foo"]}}))
    rules = load_rules_from_dir(tmp_path)
    assert [r.id for r in rules] == ["r1"]


def test_load_rules_reads_array_file(tmp_path: Path):
    p = tmp_path / "many.json"
    p.write_text(
        json.dumps(
            [
                {"id": "a", "family": "t", "match": {"tool_names": ["foo"]}},
                {"id": "b", "family": "t", "match": {"tool_names": ["bar"]}},
            ]
        )
    )
    rules = load_rules_from_dir(tmp_path)
    assert sorted(r.id for r in rules) == ["a", "b"]


def test_load_rules_missing_dir_returns_empty(tmp_path: Path):
    assert load_rules_from_dir(tmp_path / "nope") == []


def test_three_layer_merge_later_overrides_earlier(tmp_path: Path):
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    project = tmp_path / "project"
    for d in (builtin, user, project):
        d.mkdir()
    (builtin / "x.json").write_text(
        json.dumps({"id": "x", "family": "t", "priority": 1, "match": {"tool_names": ["a"]}})
    )
    (user / "x.json").write_text(
        json.dumps({"id": "x", "family": "t", "priority": 5, "match": {"tool_names": ["a"]}})
    )
    (project / "x.json").write_text(
        json.dumps({"id": "x", "family": "t", "priority": 9, "match": {"tool_names": ["a"]}})
    )
    out = load_rules(builtin, user, project)
    assert len(out) == 1
    assert out[0].priority == 9  # project layer wins


def test_load_rules_sort_is_priority_desc_then_id(tmp_path: Path):
    (tmp_path / "a.json").write_text(
        json.dumps(
            [
                {"id": "low", "family": "t", "priority": 1, "match": {"tool_names": ["x"]}},
                {"id": "hi_b", "family": "t", "priority": 9, "match": {"tool_names": ["x"]}},
                {"id": "hi_a", "family": "t", "priority": 9, "match": {"tool_names": ["x"]}},
            ]
        )
    )
    out = load_rules(tmp_path)
    assert [r.id for r in out] == ["hi_a", "hi_b", "low"]


def test_resolve_arg_dotted_path():
    assert _resolve_arg({"a": {"b": "deep"}}, "a.b") == "deep"


def test_resolve_arg_empty_path_returns_json():
    out = _resolve_arg({"a": 1}, "")
    assert '"a": 1' in out or '"a":1' in out


def test_resolve_arg_missing_segment_returns_empty():
    assert _resolve_arg({"a": 1}, "b.c") == ""


def test_classify_tool_name_filter():
    rules = [_rule("r1", tool_names=["foo"])]
    assert classify(rules, "foo", {}) is rules[0]
    assert classify(rules, "bar", {}) is None


def test_classify_arg_includes_all_must_match():
    rules = [_rule("r1", tool_names=["bash"], arg_path="cmd", arg_includes=["git", "status"])]
    assert classify(rules, "bash", {"cmd": "git status -s"}) is rules[0]
    assert classify(rules, "bash", {"cmd": "git log"}) is None


def test_classify_arg_includes_any():
    rules = [
        _rule(
            "r1",
            tool_names=["bash"],
            arg_path="cmd",
            arg_includes_any=["docker ps", "kubectl"],
        )
    ]
    assert classify(rules, "bash", {"cmd": "docker ps -a"}) is rules[0]
    assert classify(rules, "bash", {"cmd": "kubectl get pods"}) is rules[0]
    assert classify(rules, "bash", {"cmd": "git log"}) is None


def test_classify_arg_regex():
    rules = [_rule("r1", tool_names=["bash"], arg_path="cmd", arg_regex=r"^find\s+/")]
    assert classify(rules, "bash", {"cmd": "find / -name foo"}) is rules[0]
    assert classify(rules, "bash", {"cmd": "ls -la"}) is None


def test_classify_priority_first_match_wins():
    rules = sorted(
        [
            _rule("low", priority=1, tool_names=["x"]),
            _rule("hi", priority=9, tool_names=["x"]),
        ],
        key=lambda r: (-r.priority, r.id),
    )
    assert classify(rules, "x", {}).id == "hi"


def test_rule_extra_fields_rejected_by_pydantic():
    with pytest.raises(Exception):
        Rule.model_validate(
            {"id": "r", "family": "t", "match": {"tool_names": ["a"]}, "extra_field": 1}
        )
