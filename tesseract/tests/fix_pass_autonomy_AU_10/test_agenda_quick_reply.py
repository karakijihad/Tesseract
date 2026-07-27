"""AU-10 — inbound ``<agenda_id>:<verb>`` quick-reply tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tesseract.integrations.telegram.agenda_quick_reply import (
    SNOOZE_PRIORITY_DELTA,
    SNOOZE_PRIORITY_FLOOR,
    apply_quick_reply,
    format_reply_body,
    looks_like_quick_reply,
    parse_quick_reply,
)
from tesseract.orchestrator.autonomy import AgendaStore
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    ApprovalGate,
    RiskClass,
    mint_agenda_id,
)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def store(isolated_home: Path) -> AgendaStore:
    return AgendaStore()


def _make_item(
    store: AgendaStore,
    *,
    goal: str = "do the thing",
    risk_class: RiskClass = RiskClass.PROPOSE,
    approvals: list[ApprovalGate] | None = None,
) -> AgendaItem:
    now = datetime.now(timezone.utc)
    item = AgendaItem(
        id=f"{mint_agenda_id(goal[:30], now=now)}-abcd",
        created_at=now,
        updated_at=now,
        source=AgendaSource.SELF_REFLECTION,
        goal=goal,
        risk_class=risk_class,
        approvals_required=approvals or [],
    )
    store.add(item)
    return item


# -- parser ---------------------------------------------------------------


def test_parser_matches_valid_strings():
    r = parse_quick_reply("ag-2026-05-18-1234-do-thing:approve")
    assert r is not None
    assert r.agenda_id == "ag-2026-05-18-1234-do-thing"
    assert r.verb == "approve"

    r2 = parse_quick_reply("ag-2026-05-18-1234-do-thing:DENY")
    assert r2 is not None and r2.verb == "deny"

    r3 = parse_quick_reply("  ag-2026-05-18-1234-do:snooze  ")
    assert r3 is not None and r3.verb == "snooze"


def test_parser_rejects_non_matching():
    for text in (
        "",
        "hello",
        "/missions",
        "ag-bad-shape:approve",
        "ag-2026-05-18-1234-do:explode",
        "approve:ag-2026-05-18-1234-do",
        "ag-2026-05-18-1234-do",
    ):
        assert parse_quick_reply(text) is None, f"expected miss for {text!r}"


def test_looks_like_pre_check_is_cheap_and_consistent():
    assert looks_like_quick_reply("ag-2026-05-18-1234-do:approve") is True
    assert looks_like_quick_reply("hello world") is False
    assert looks_like_quick_reply("AG-2026-05-18-1234-do:Approve") is True
    assert looks_like_quick_reply("ag-2026-05-18-1234-do") is False


# -- apply ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_fulfils_gates(store: AgendaStore):
    item = _make_item(
        store,
        risk_class=RiskClass.PROPOSE,
        approvals=[ApprovalGate(kind="config_apply", target="weights.yaml")],
    )
    reply = parse_quick_reply(f"{item.id}:approve")
    assert reply is not None
    result = await apply_quick_reply(reply, store=store)
    assert result["ok"] is True
    assert result["verb"] == "approve"
    assert result["fulfilled_count"] == 1
    refreshed = store.get(item.id)
    assert refreshed is not None
    assert all(g.fulfilled for g in refreshed.approvals_required)


@pytest.mark.asyncio
async def test_deny_cancels_item(store: AgendaStore):
    item = _make_item(store)
    reply = parse_quick_reply(f"{item.id}:deny")
    assert reply is not None
    result = await apply_quick_reply(reply, store=store)
    assert result["ok"] is True
    refreshed = store.get(item.id)
    assert refreshed is not None
    assert refreshed.status == AgendaStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_verb_also_cancels(store: AgendaStore):
    item = _make_item(store)
    reply = parse_quick_reply(f"{item.id}:cancel")
    assert reply is not None
    result = await apply_quick_reply(reply, store=store)
    assert result["ok"] is True
    refreshed = store.get(item.id)
    assert refreshed is not None
    assert refreshed.status == AgendaStatus.CANCELLED


@pytest.mark.asyncio
async def test_snooze_drops_operator_priority(store: AgendaStore):
    item = _make_item(store)
    item.operator_priority = 1
    store.save(item)
    reply = parse_quick_reply(f"{item.id}:snooze")
    assert reply is not None
    result = await apply_quick_reply(reply, store=store)
    assert result["ok"] is True
    assert result["operator_priority"] == 1 - SNOOZE_PRIORITY_DELTA
    refreshed = store.get(item.id)
    assert refreshed is not None
    assert refreshed.operator_priority == 1 - SNOOZE_PRIORITY_DELTA


@pytest.mark.asyncio
async def test_snooze_clamped_at_floor(store: AgendaStore):
    item = _make_item(store)
    item.operator_priority = SNOOZE_PRIORITY_FLOOR
    store.save(item)
    reply = parse_quick_reply(f"{item.id}:snooze")
    assert reply is not None
    result = await apply_quick_reply(reply, store=store)
    assert result["ok"] is True
    assert result.get("noop") is True
    assert result["operator_priority"] == SNOOZE_PRIORITY_FLOOR


@pytest.mark.asyncio
async def test_unknown_id_returns_not_found(store: AgendaStore):
    reply = parse_quick_reply("ag-2026-05-18-9999-missing:approve")
    assert reply is not None
    result = await apply_quick_reply(reply, store=store)
    assert result["ok"] is False
    assert result["reason"] == "not_found"


@pytest.mark.asyncio
async def test_terminal_item_blocks_action(store: AgendaStore):
    item = _make_item(store)
    store.transition(item, AgendaStatus.DONE, reason="test")
    reply = parse_quick_reply(f"{item.id}:approve")
    assert reply is not None
    result = await apply_quick_reply(reply, store=store)
    assert result["ok"] is False
    assert result["reason"] == "already_terminal"


def test_format_reply_body_renders_safe_text():
    ok = {
        "ok": True, "verb": "approve",
        "agenda_id": "ag-2026-05-18-1200-x",
        "goal": "do the thing",
    }
    body = format_reply_body(ok)
    assert "Approve" in body
    assert "gates fulfilled" in body

    err = {"ok": False, "agenda_id": "ag-x", "reason": "not_found"}
    body2 = format_reply_body(err)
    assert "not_found" in body2
