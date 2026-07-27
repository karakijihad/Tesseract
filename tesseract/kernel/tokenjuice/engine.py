"""TokenJuice processing entry — classify → transforms → reducer chain → audit.

Pure orchestration over the reducer engine. The single public entry point
is `process()`; `tesseract/brain/tools.py::execute_tool` calls it once per
tool result.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from .audit import count_tokens, write_audit
from .reducers import TRANSFORMS, apply_reducer
from .rules import Rule, classify

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessResult:
    text: str
    rule_id: str
    tokens_before: int
    tokens_after: int
    applied: bool
    reason: str = ""


def _apply_rule_chain(rule: Rule, text: str) -> str:
    cur = text
    for tname in rule.transforms:
        fn = TRANSFORMS.get(tname)
        if fn is None:
            raise ValueError(f"unknown transform: {tname}")
        cur = fn(cur)
    for step in rule.reducers:
        kind = step.get("kind")
        if not isinstance(kind, str):
            raise ValueError(f"reducer step missing 'kind': {step}")
        if kind == "passthrough":
            continue
        params = {k: v for k, v in step.items() if k != "kind"}
        cur = apply_reducer(kind, cur, params)
    return cur


def _passthrough_by_size(rule: Rule, text: str) -> bool:
    pw = rule.passthrough_when
    if pw.max_chars and len(text) <= pw.max_chars:
        return True
    if pw.max_lines and (text.count("\n") + 1) <= pw.max_lines:
        return True
    return False


def _record_audit(
    *,
    tool: str,
    rule_id: str,
    tokens_before: int,
    tokens_after: int,
    dry_run: bool,
    applied: bool,
    reason: str,
) -> None:
    try:
        write_audit(
            {
                "ts": int(time.time() * 1000),
                "tool": tool,
                "rule_id": rule_id,
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
                "dry_run": dry_run,
                "applied": applied,
                "reason": reason,
            }
        )
    except OSError as exc:
        logger.warning("tokenjuice audit append failed: %s", exc)


def _passthrough(
    text: str,
    rule_id: str,
    tokens: int,
    reason: str,
    *,
    tool_name: str,
    dry_run: bool,
    audit_log: bool,
) -> ProcessResult:
    """Return an unapplied ProcessResult and optionally write an audit record."""
    if audit_log:
        _record_audit(
            tool=tool_name,
            rule_id=rule_id,
            tokens_before=tokens,
            tokens_after=tokens,
            dry_run=dry_run,
            applied=False,
            reason=reason,
        )
    return ProcessResult(
        text=text,
        rule_id=rule_id,
        tokens_before=tokens,
        tokens_after=tokens,
        applied=False,
        reason=reason,
    )


def process(
    text: str,
    tool_name: str,
    tool_args: Any,
    *,
    rules: list[Rule],
    enabled: bool = True,
    dry_run: bool = False,
    audit_log: bool = True,
    disabled_rules: dict[str, list[str]] | None = None,
) -> ProcessResult:
    """Classify (tool_name, tool_args), apply the matched rule chain, audit.

    Behavior matrix:
    - `enabled=False`             → passthrough, no audit, no rule lookup.
    - No matching rule            → passthrough, no audit (cheap path).
    - Rule disabled per-tool      → passthrough, audit reason=disabled_per_tool.
    - `passthrough_when` triggers → passthrough, audit reason=below_threshold.
    - Reducer chain raises        → passthrough, audit reason=apply_failed.
    - `dry_run=True`              → audit both versions; return original.
    - Otherwise                   → audit + apply; return reduced text.
    """
    tokens_before = count_tokens(text)

    if not enabled:
        return ProcessResult(
            text=text,
            rule_id="",
            tokens_before=tokens_before,
            tokens_after=tokens_before,
            applied=False,
            reason="disabled_global",
        )

    rule = classify(rules, tool_name, tool_args)
    if rule is None:
        return ProcessResult(
            text=text,
            rule_id="",
            tokens_before=tokens_before,
            tokens_after=tokens_before,
            applied=False,
            reason="no_match",
        )

    _pt = dict(tool_name=tool_name, dry_run=dry_run, audit_log=audit_log)

    disabled = (disabled_rules or {}).get(tool_name, [])
    if rule.id in disabled:
        return _passthrough(text, rule.id, tokens_before, "disabled_per_tool", **_pt)

    if _passthrough_by_size(rule, text):
        return _passthrough(text, rule.id, tokens_before, "below_threshold", **_pt)

    try:
        reduced = _apply_rule_chain(rule, text)
    except (ValueError, TypeError, re.error) as exc:
        # TypeError covers malformed user/project rules — e.g. {"n": "bad"} on
        # head_lines, where the chain dispatch reaches the reducer with the
        # wrong arg type. Still records the apply_failed audit row.
        logger.warning("tokenjuice rule %s failed on %s: %s", rule.id, tool_name, exc)
        return _passthrough(text, rule.id, tokens_before, "apply_failed", **_pt)

    if not reduced and rule.on_empty:
        reduced = rule.on_empty

    tokens_after = count_tokens(reduced)
    final_text = text if dry_run else reduced
    final_applied = not dry_run

    if audit_log:
        _record_audit(
            tool=tool_name,
            rule_id=rule.id,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            dry_run=dry_run,
            applied=final_applied,
            reason="ok",
        )

    return ProcessResult(
        text=final_text,
        rule_id=rule.id,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        applied=final_applied,
        reason="ok",
    )
