"""Rule model + loader + classifier.

Schema is ported from vincentkoc/tokenjuice with TARS-specific reducer
kinds. Three-layer merge (builtin → user → project) is keyed by `id`;
later layers override. Classification is priority-ordered (high → low),
first match wins.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RuleMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_names: list[str]
    arg_path: str = ""
    arg_includes: list[str] = Field(default_factory=list)
    arg_includes_any: list[str] = Field(default_factory=list)
    arg_regex: str = ""


class PassthroughWhen(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_chars: int = 0
    max_lines: int = 0


class FailPreserve(BaseModel):
    model_config = ConfigDict(extra="forbid")

    head: int = 0
    tail: int = 0


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    family: str
    description: str = ""
    priority: int = 0
    match: RuleMatch
    transforms: list[str] = Field(default_factory=list)
    reducers: list[dict[str, Any]] = Field(default_factory=list)
    passthrough_when: PassthroughWhen = Field(default_factory=PassthroughWhen)
    fail_preserve: FailPreserve = Field(default_factory=FailPreserve)
    on_empty: str = ""


def load_rules_from_dir(p: Path) -> list[Rule]:
    """Read every `*.json` file in `p`; each file may hold one rule or a list."""
    if not p.exists() or not p.is_dir():
        return []
    rules: list[Rule] = []
    for f in sorted(p.glob("*.json")):
        raw = json.loads(f.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else [raw]
        for item in items:
            rules.append(Rule.model_validate(item))
    return rules


def load_rules(builtin: Path, user: Path | None = None, project: Path | None = None) -> list[Rule]:
    """Three-layer merge keyed by `id`. Later layers override earlier ones.

    Returns the merged set sorted by `-priority` then `id` (highest priority
    first; deterministic tie-break by id).
    """
    layers = [p for p in (builtin, user, project) if p is not None]
    by_id: dict[str, Rule] = {}
    for layer in layers:
        for r in load_rules_from_dir(layer):
            by_id[r.id] = r
    return sorted(by_id.values(), key=lambda r: (-r.priority, r.id))


def _resolve_arg(tool_args: Any, path: str) -> str:
    """Walk `path` (dotted) into tool_args; return the value coerced to str.

    Empty `path` returns a JSON-stringified view of the whole args dict
    (so rules can match against any field by full-text contains).
    """
    if not path:
        if isinstance(tool_args, dict):
            return json.dumps(tool_args, sort_keys=True, ensure_ascii=False)
        return str(tool_args or "")
    cur: Any = tool_args
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return ""
    if isinstance(cur, (dict, list)):
        return json.dumps(cur, sort_keys=True, ensure_ascii=False)
    return str(cur if cur is not None else "")


def classify(rules: list[Rule], tool_name: str, tool_args: Any) -> Rule | None:
    """Return the highest-priority rule that matches; None if no rule fits."""
    for rule in rules:
        if tool_name not in rule.match.tool_names:
            continue
        arg_text = _resolve_arg(tool_args, rule.match.arg_path)
        if rule.match.arg_includes and not all(s in arg_text for s in rule.match.arg_includes):
            continue
        if rule.match.arg_includes_any and not any(s in arg_text for s in rule.match.arg_includes_any):
            continue
        if rule.match.arg_regex and not re.search(rule.match.arg_regex, arg_text):
            continue
        return rule
    return None
