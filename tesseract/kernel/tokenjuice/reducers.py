"""Pure reducer + transform functions — no I/O, no LLM calls.

Transforms are idempotent normalisations (always-on per rule). Reducers
are the compression chain (each step shrinks). The optional `summarize`
reducer is *not* here — it lives behind a guarded call into the
subagents_default adapter and is only invoked when explicitly named.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def dedup_adjacent(text: str) -> str:
    out: list[str] = []
    prev: str | None = None
    for line in text.splitlines():
        if line != prev:
            out.append(line)
        prev = line
    return "\n".join(out)


def trim_empty_edges(text: str) -> str:
    return text.strip("\r\n")


def pretty_json(text: str) -> str:
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text
    return json.dumps(obj, indent=2, sort_keys=False, ensure_ascii=False)


TRANSFORMS: dict[str, Callable[[str], str]] = {
    "strip_ansi": strip_ansi,
    "dedup_adjacent": dedup_adjacent,
    "trim_empty_edges": trim_empty_edges,
    "pretty_json": pretty_json,
}


def head_lines(text: str, *, n: int) -> str:
    return "\n".join(text.splitlines()[:n])


def tail_lines(text: str, *, n: int) -> str:
    return "\n".join(text.splitlines()[-n:])


def head_tail(text: str, *, head: int, tail: int) -> str:
    lines = text.splitlines()
    if len(lines) <= head + tail:
        return text
    elided = len(lines) - head - tail
    return "\n".join([*lines[:head], f"… ({elided} lines elided) …", *lines[-tail:]])


def dedup_lines(text: str) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for line in text.splitlines():
        if line not in seen:
            out.append(line)
            seen.add(line)
    return "\n".join(out)


def drop_regex(text: str, *, patterns: list[str]) -> str:
    compiled = [re.compile(p) for p in patterns]
    return "\n".join(
        line for line in text.splitlines() if not any(p.search(line) for p in compiled)
    )


def cap_chars(text: str, *, n: int) -> str:
    if len(text) <= n:
        return text
    return text[:n] + f"\n… (truncated; {len(text) - n} chars elided)"


def cap_lines(text: str, *, n: int) -> str:
    lines = text.splitlines()
    if len(lines) <= n:
        return text
    return "\n".join(lines[:n]) + f"\n… (truncated; {len(lines) - n} lines elided)"


def passthrough(text: str) -> str:
    return text


REDUCERS: dict[str, Callable[..., str]] = {
    "head_lines": head_lines,
    "tail_lines": tail_lines,
    "head_tail": head_tail,
    "dedup_lines": dedup_lines,
    "drop_regex": drop_regex,
    "cap_chars": cap_chars,
    "cap_lines": cap_lines,
    "passthrough": passthrough,
}


def apply_reducer(kind: str, text: str, params: dict[str, Any]) -> str:
    """Dispatch helper — looks up `kind` in REDUCERS, applies."""
    fn = REDUCERS.get(kind)
    if fn is None:
        raise ValueError(f"unknown reducer kind: {kind}")
    return fn(text, **params)
