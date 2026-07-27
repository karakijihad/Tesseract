"""AU-15: engine.process() behavior matrix + audit log shape."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tesseract.kernel.tokenjuice.engine import ProcessResult, process
from tesseract.kernel.tokenjuice.rules import Rule


def _rule(*, reducers=None, transforms=None, passthrough_chars=0, on_empty="") -> Rule:
    return Rule.model_validate(
        {
            "id": "test.rule",
            "family": "t",
            "priority": 0,
            "match": {"tool_names": ["test_tool"]},
            "transforms": transforms or [],
            "reducers": reducers or [],
            "passthrough_when": {"max_chars": passthrough_chars},
            "on_empty": on_empty,
        }
    )


def _audit_lines(tmp_path: Path) -> list[dict]:
    p = tmp_path / "logs" / "tokenjuice" / "audit.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line]


def test_disabled_global_passthrough_no_audit(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    pr = process("hi", "test_tool", {}, rules=[_rule()], enabled=False)
    assert pr.text == "hi"
    assert pr.applied is False
    assert pr.reason == "disabled_global"
    assert _audit_lines(tmp_path) == []


def test_no_match_passthrough_no_audit(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    pr = process("hi", "unknown_tool", {}, rules=[_rule()])
    assert pr.applied is False
    assert pr.rule_id == ""
    assert pr.reason == "no_match"
    assert _audit_lines(tmp_path) == []


def test_below_threshold_emits_audit_but_doesnt_apply(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    rule = _rule(reducers=[{"kind": "head_lines", "n": 1}], passthrough_chars=100)
    pr = process("short", "test_tool", {}, rules=[rule])
    assert pr.text == "short"
    assert pr.applied is False
    assert pr.reason == "below_threshold"
    rows = _audit_lines(tmp_path)
    assert len(rows) == 1
    assert rows[0]["reason"] == "below_threshold"
    assert rows[0]["tokens_before"] == rows[0]["tokens_after"]


def test_reducer_chain_applies_and_audits(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    rule = _rule(reducers=[{"kind": "head_lines", "n": 2}])
    text = "a\nb\nc\nd\ne"
    pr = process(text, "test_tool", {}, rules=[rule])
    assert pr.text == "a\nb"
    assert pr.applied is True
    assert pr.reason == "ok"
    rows = _audit_lines(tmp_path)
    assert len(rows) == 1
    assert rows[0]["applied"] is True
    assert rows[0]["tokens_after"] < rows[0]["tokens_before"]


def test_dry_run_returns_original_but_audits_both(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    rule = _rule(reducers=[{"kind": "head_lines", "n": 1}])
    text = "\n".join("x" * 20 for _ in range(8))  # ~170 chars → ~40 tokens
    pr = process(text, "test_tool", {}, rules=[rule], dry_run=True)
    assert pr.text == text  # original returned
    assert pr.applied is False
    rows = _audit_lines(tmp_path)
    assert rows[0]["dry_run"] is True
    assert rows[0]["applied"] is False
    assert rows[0]["tokens_after"] < rows[0]["tokens_before"]  # delta still recorded


def test_per_tool_disabled_rule_passthrough(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    rule = _rule(reducers=[{"kind": "head_lines", "n": 1}])
    pr = process(
        "a\nb\nc",
        "test_tool",
        {},
        rules=[rule],
        disabled_rules={"test_tool": ["test.rule"]},
    )
    assert pr.text == "a\nb\nc"
    assert pr.applied is False
    assert pr.reason == "disabled_per_tool"
    rows = _audit_lines(tmp_path)
    assert len(rows) == 1
    assert rows[0]["reason"] == "disabled_per_tool"


def test_apply_failed_passthrough_audits(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    # Unknown reducer kind raises inside _apply_rule_chain.
    rule = _rule(reducers=[{"kind": "nonexistent_reducer"}])
    pr = process("a\nb\nc", "test_tool", {}, rules=[rule])
    assert pr.text == "a\nb\nc"
    assert pr.applied is False
    assert pr.reason == "apply_failed"
    rows = _audit_lines(tmp_path)
    assert rows[0]["reason"] == "apply_failed"


def test_apply_failed_catches_typeerror_from_malformed_params(tmp_path: Path, monkeypatch):
    # A malformed user/project rule with the wrong arg type raises TypeError
    # inside the reducer (e.g., head_lines(n="bad")). Engine must catch it
    # and record apply_failed, not propagate.
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    rule = _rule(reducers=[{"kind": "head_lines", "n": "not-an-int"}])
    pr = process("a\nb\nc\nd", "test_tool", {}, rules=[rule])
    assert pr.applied is False
    assert pr.reason == "apply_failed"
    rows = _audit_lines(tmp_path)
    assert rows[0]["reason"] == "apply_failed"


def test_audit_disabled_writes_nothing(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    rule = _rule(reducers=[{"kind": "head_lines", "n": 1}])
    pr = process("a\nb\nc", "test_tool", {}, rules=[rule], audit_log=False)
    assert pr.applied is True
    assert _audit_lines(tmp_path) == []


def test_on_empty_replaces_empty_reducer_output(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    rule = _rule(
        reducers=[{"kind": "drop_regex", "patterns": [".*"]}],
        on_empty="(nothing left)",
    )
    pr = process("a\nb", "test_tool", {}, rules=[rule])
    assert pr.text == "(nothing left)"
    assert pr.applied is True


def test_transforms_run_before_reducers(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    rule = _rule(
        transforms=["strip_ansi"],
        reducers=[{"kind": "head_lines", "n": 1}],
    )
    pr = process("\x1b[31mred\x1b[0m\nblue", "test_tool", {}, rules=[rule])
    assert pr.text == "red"
    assert pr.applied is True


def test_passthrough_reducer_is_noop(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    rule = _rule(reducers=[{"kind": "passthrough"}])
    long_text = "a\n" * 200
    pr = process(long_text, "test_tool", {}, rules=[rule])
    # No reduction so text identical; passthrough kind is intentionally a no-op.
    assert pr.text == long_text
    assert pr.applied is True
