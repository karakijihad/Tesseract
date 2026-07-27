"""MO-10-3 §2c — fan-out filters allowlist + isolates per-recipient failures."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from tesseract.integrations.telegram.brief_push import send_to_operators


@dataclass
class _Allowlist:
    chat_ids: set = field(default_factory=set)
    pending: dict = field(default_factory=dict)
    blocked: set = field(default_factory=set)


class _CollectingBridge:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail_for: set[str] = set()

    async def send_text(self, *, chat_ref: str, text: str) -> None:
        if chat_ref in self.fail_for:
            raise RuntimeError(f"simulated failure for {chat_ref}")
        self.sent.append((chat_ref, text))


def test_push_to_every_operator_tier_chat():
    bridge = _CollectingBridge()
    al = _Allowlist(chat_ids={111, 222, 333})
    res = asyncio.run(send_to_operators(
        "hello", bridge=bridge, allowlist=al,
        user_tier={"111": "operator", "222": "operator", "333": "operator"},
    ))
    assert res["sent"] == 3
    assert res["errors"] == 0
    assert {c for c, _ in bridge.sent} == {"111", "222", "333"}


def test_push_excludes_blocked():
    bridge = _CollectingBridge()
    al = _Allowlist(chat_ids={111, 222}, blocked={222})
    res = asyncio.run(send_to_operators(
        "hi", bridge=bridge, allowlist=al,
        user_tier={"111": "operator", "222": "operator"},
    ))
    assert res["sent"] == 1
    assert bridge.sent == [("111", "hi")]


def test_push_excludes_pending():
    bridge = _CollectingBridge()
    al = _Allowlist(chat_ids={111}, pending={222: object()})
    res = asyncio.run(send_to_operators(
        "hi", bridge=bridge, allowlist=al, user_tier={"111": "operator"},
    ))
    assert res["sent"] == 1


def test_push_excludes_non_operator_tier():
    bridge = _CollectingBridge()
    al = _Allowlist(chat_ids={111, 222})
    res = asyncio.run(send_to_operators(
        "hi", bridge=bridge, allowlist=al,
        user_tier={"111": "operator", "222": "friend"},
    ))
    assert res["sent"] == 1
    assert bridge.sent == [("111", "hi")]


def test_failure_isolation_does_not_block_other_recipients():
    bridge = _CollectingBridge()
    bridge.fail_for = {"111"}
    al = _Allowlist(chat_ids={111, 222})
    res = asyncio.run(send_to_operators(
        "hi", bridge=bridge, allowlist=al,
        user_tier={"111": "operator", "222": "operator"},
    ))
    assert res["sent"] == 1
    assert res["errors"] == 1
    assert bridge.sent == [("222", "hi")]


def test_empty_allowlist_returns_zero():
    bridge = _CollectingBridge()
    al = _Allowlist()
    res = asyncio.run(send_to_operators(
        "hi", bridge=bridge, allowlist=al, user_tier={},
    ))
    assert res == {"sent": 0, "skipped": 0, "errors": 0}
