"""Approving ``soul_proposal`` / ``feedback_proposal`` MUST execute the action.

Before this fix-pass the workspace ``post_decision`` handler only flipped
the event's status to ``approved`` for these two kinds — the underlying
mutation (SOUL.md bullet append, memory merge/archive) was never
performed. Tests reproduce the gap by approving real events and then
inspecting the side-effect surface (SOUL.md for soul, MemoryStore for
feedback). With the fix in place the same harness asserts the mutations
land and the event flips to ``applied``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.memory_promote import MemoryPromoteTool
from tesseract.kernel.tools.soul_growth_propose import SoulGrowthProposeTool
from tesseract.memory.index import MemoryIndex
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType, Stability
from tesseract.mirror.server.routes import workspace as ws_routes
from tesseract.workspace_events import EventStore, WorkspaceEvent


_SOUL_SEED = """# SOUL

## Growth

- Existing bullet that should remain.
"""


class _StubReq:
    def __init__(
        self,
        store: EventStore,
        *,
        match: dict | None = None,
        body: dict | None = None,
        tool_registry=None,
    ) -> None:
        self.app = {
            "workspace_event_store": store,
            "tool_registry": tool_registry,
            "server_sessions": {},
        }
        self.match_info = match or {}
        self.query = {}
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _resp_json(resp) -> dict:
    return json.loads(resp.body.decode("utf-8"))


def _seed_soul() -> Path:
    """Seed SOUL.md under `workspace_dir()` — the caller must have already
    set `TESSERACT_HOME` (via the `isolated_home` fixture)."""
    from tesseract.paths import workspace_dir
    target = workspace_dir() / "SOUL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_SOUL_SEED, encoding="utf-8")
    return target


def _seed_memory(store: MemoryStore, *, mem_id: str, body: str, importance: int = 7) -> None:
    fm = MemoryFrontmatter(
        id=mem_id,
        type=MemoryType.FEEDBACK,
        title=f"title-{mem_id}",
        summary=body[:80],
        importance=importance,
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        stability=Stability.ACTIVE,
        auto_links=[],
    )
    target = store.store_dir / "feedback" / f"{mem_id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\n"
        + yaml.dump(fm.to_yaml_dict(), default_flow_style=False, sort_keys=False)
        + "---\n\n"
        + body,
        encoding="utf-8",
    )


def _registry_with_memory_promote(home: Path) -> SimpleNamespace:
    store_dir = home / "memory-store"
    store_dir.mkdir(parents=True, exist_ok=True)
    store = MemoryStore(store_dir)
    index = MemoryIndex(store_dir=store_dir)
    soul = SoulGrowthProposeTool(repo_root=home)
    tool = MemoryPromoteTool(store=store, index=index, soul_growth_tool=soul)
    return SimpleNamespace(
        get=lambda name: tool if name == "memory_promote" else None,
        tools={"memory_promote": tool},
        _store=store,
    )


# ───────────────────────── soul_proposal ─────────────────────────


@pytest.mark.asyncio
async def test_approve_soul_proposal_appends_to_soul_md(
    isolated_home: Path,
) -> None:
    _seed_soul()
    store = EventStore(isolated_home / "logs")
    ev = store.append_event(WorkspaceEvent.new(
        kind="soul_proposal",
        source="feedback_consolidator",
        title="Soul-growth bullet (×3)",
        summary="operator prefers terse",
        payload={
            "action": "propose_soul_growth",
            "bullet": "Operator prefers terse summaries with no closing recap.",
            "supporting_ids": ["mem_a", "mem_b", "mem_c"],
        },
    ))

    req = _StubReq(store, match={"event_id": ev.event_id}, body={"decision": "approve"})
    resp = await ws_routes.post_decision(req)

    assert resp.status == 200
    data = _resp_json(resp)
    assert data["status"] == "applied"

    from tesseract.paths import workspace_dir
    soul_text = (workspace_dir() / "SOUL.md").read_text(encoding="utf-8")
    assert "Operator prefers terse summaries with no closing recap." in soul_text
    # Existing bullet stays.
    assert "Existing bullet that should remain." in soul_text


@pytest.mark.asyncio
async def test_approve_soul_proposal_missing_bullet_is_400(
    isolated_home: Path,
) -> None:
    _seed_soul()
    store = EventStore(isolated_home / "logs")
    ev = store.append_event(WorkspaceEvent.new(
        kind="soul_proposal",
        source="feedback_consolidator",
        title="bad",
        summary="",
        payload={"action": "propose_soul_growth", "bullet": "   "},
    ))

    req = _StubReq(store, match={"event_id": ev.event_id}, body={"decision": "approve"})
    resp = await ws_routes.post_decision(req)
    assert resp.status == 400
    # Event must stay pending so the operator can fix and retry.
    pending = store.list_events(status="pending")
    assert ev.event_id in [e.event_id for e in pending]


# ───────────────────────── feedback_proposal (merge_into) ─────────────────────────


@pytest.mark.asyncio
async def test_approve_feedback_proposal_merge_into_executes(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ws_routes, "ROOT", isolated_home)
    registry = _registry_with_memory_promote(isolated_home)
    mem_store: MemoryStore = registry._store
    _seed_memory(
        mem_store, mem_id="mem_keep",
        body="Operator wants the keeper record to carry the merged context across all duplicate variants.",
        importance=7,
    )
    _seed_memory(
        mem_store, mem_id="mem_absorb_1",
        body="Alpha variant — operator phrased the same directive about merging duplicate feedback entries.",
        importance=5,
    )
    _seed_memory(
        mem_store, mem_id="mem_absorb_2",
        body="Beta variant — operator paraphrased the merge rule on a separate day with similar wording.",
        importance=8,
    )

    store = EventStore(isolated_home / "logs")
    ev = store.append_event(WorkspaceEvent.new(
        kind="feedback_proposal",
        source="feedback_consolidator",
        title="Merge 2 → mem_keep",
        summary="duplicates",
        payload={
            "action": "merge_into",
            "keep": "mem_keep",
            "absorb": ["mem_absorb_1", "mem_absorb_2"],
        },
    ))

    req = _StubReq(
        store,
        match={"event_id": ev.event_id},
        body={"decision": "approve"},
        tool_registry=registry,
    )
    resp = await ws_routes.post_decision(req)

    assert resp.status == 200
    data = _resp_json(resp)
    assert data["status"] == "applied"

    keep_fm, keep_body = mem_store.read("mem_keep")
    assert "Alpha variant" in keep_body
    assert "Beta variant" in keep_body
    # Importance lifts to max of all three (8).
    assert keep_fm.importance == 8

    for src in ("mem_absorb_1", "mem_absorb_2"):
        fm, _ = mem_store.read(src)
        assert fm.stability == Stability.ARCHIVED


@pytest.mark.asyncio
async def test_approve_feedback_proposal_archive_executes(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ws_routes, "ROOT", isolated_home)
    registry = _registry_with_memory_promote(isolated_home)
    mem_store: MemoryStore = registry._store
    _seed_memory(
        mem_store, mem_id="mem_stale",
        body="Stale directive about archiving superseded entries when the operator approves it explicitly.",
        importance=4,
    )

    store = EventStore(isolated_home / "logs")
    ev = store.append_event(WorkspaceEvent.new(
        kind="feedback_proposal",
        source="feedback_consolidator",
        title="Archive mem_stale",
        summary="superseded",
        payload={"action": "archive", "memory_id": "mem_stale"},
    ))

    req = _StubReq(
        store,
        match={"event_id": ev.event_id},
        body={"decision": "approve"},
        tool_registry=registry,
    )
    resp = await ws_routes.post_decision(req)

    assert resp.status == 200
    fm, _ = mem_store.read("mem_stale")
    assert fm.stability == Stability.ARCHIVED


@pytest.mark.asyncio
async def test_approve_feedback_proposal_without_registry_returns_503(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the tool registry hasn't booted (early-Mirror, CLI), approval
    must NOT silently mark the event applied — surface a clear 503 so the
    operator knows to retry once the registry is up."""
    monkeypatch.setattr(ws_routes, "ROOT", isolated_home)
    store = EventStore(isolated_home / "logs")
    ev = store.append_event(WorkspaceEvent.new(
        kind="feedback_proposal",
        source="feedback_consolidator",
        title="Archive mem_x",
        summary="",
        payload={"action": "archive", "memory_id": "mem_x"},
    ))

    req = _StubReq(
        store,
        match={"event_id": ev.event_id},
        body={"decision": "approve"},
        tool_registry=None,
    )
    resp = await ws_routes.post_decision(req)
    assert resp.status == 503
    pending = store.list_events(status="pending")
    assert ev.event_id in [e.event_id for e in pending]


@pytest.mark.asyncio
async def test_approve_feedback_proposal_merge_into_is_all_or_nothing(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any failing absorb leaves the event pending so the operator can
    investigate. The successful absorbs are durably archived on disk
    (merge ran), and a re-approve after fixing the broken source is
    idempotent thanks to ``memory_promote._merge``'s archived-source
    handling."""
    monkeypatch.setattr(ws_routes, "ROOT", isolated_home)
    registry = _registry_with_memory_promote(isolated_home)
    mem_store: MemoryStore = registry._store
    _seed_memory(
        mem_store, mem_id="mem_keep",
        body="Operator directive about merging duplicate feedback entries cleanly.",
        importance=7,
    )
    _seed_memory(
        mem_store, mem_id="mem_real",
        body="A real duplicate that should absorb into the keeper record.",
        importance=5,
    )
    # mem_ghost is referenced in the payload but does not exist in store.

    store = EventStore(isolated_home / "logs")
    ev = store.append_event(WorkspaceEvent.new(
        kind="feedback_proposal",
        source="feedback_consolidator",
        title="Merge with one broken source",
        summary="",
        payload={
            "action": "merge_into",
            "keep": "mem_keep",
            "absorb": ["mem_real", "mem_ghost"],
        },
    ))

    req = _StubReq(
        store,
        match={"event_id": ev.event_id},
        body={"decision": "approve"},
        tool_registry=registry,
    )
    resp = await ws_routes.post_decision(req)
    assert resp.status == 500
    body = _resp_json(resp)
    assert body["error"] == "merge_failed"
    # Event MUST stay pending so the operator can fix and retry.
    pending = store.list_events(status="pending")
    assert ev.event_id in [e.event_id for e in pending]


@pytest.mark.asyncio
async def test_merge_into_is_idempotent_on_archived_source(
    isolated_home: Path,
) -> None:
    """Replay must not double-append a body that was already absorbed.

    Direct test on ``memory_promote`` because it is the primitive both
    the live handler and the replay script depend on for safe retry.
    """
    from tesseract.kernel.tools.memory_promote import MemoryPromoteInput
    from tesseract.memory.types import Stability

    registry = _registry_with_memory_promote(isolated_home)
    mem_store: MemoryStore = registry._store
    tool = registry.get("memory_promote")
    _seed_memory(
        mem_store, mem_id="mem_keep",
        body="Operator wants the keeper record to carry the merged context.",
        importance=7,
    )
    _seed_memory(
        mem_store, mem_id="mem_dup",
        body=(
            "Duplicate body that should land in the keeper after merge "
            "without being double-appended on retry of the same approval."
        ),
        importance=5,
    )

    first = await tool.run(
        MemoryPromoteInput(memory_id="mem_dup", action="merge_into", target="mem_keep"),
        ToolContext(),
    )
    assert first.is_error is False
    _, body_after_first = mem_store.read("mem_keep")
    occurrences_first = body_after_first.count("Duplicate body that should land")
    assert occurrences_first == 1

    second = await tool.run(
        MemoryPromoteInput(memory_id="mem_dup", action="merge_into", target="mem_keep"),
        ToolContext(),
    )
    assert second.is_error is False
    assert "no-op" in second.output.lower()
    _, body_after_second = mem_store.read("mem_keep")
    occurrences_second = body_after_second.count("Duplicate body that should land")
    assert occurrences_second == 1

    fm, _ = mem_store.read("mem_dup")
    assert fm.stability == Stability.ARCHIVED


@pytest.mark.asyncio
async def test_approve_feedback_proposal_unknown_action_is_400(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ws_routes, "ROOT", isolated_home)
    registry = _registry_with_memory_promote(isolated_home)
    store = EventStore(isolated_home / "logs")
    ev = store.append_event(WorkspaceEvent.new(
        kind="feedback_proposal",
        source="feedback_consolidator",
        title="weird",
        summary="",
        payload={"action": "supernova"},
    ))
    req = _StubReq(
        store,
        match={"event_id": ev.event_id},
        body={"decision": "approve"},
        tool_registry=registry,
    )
    resp = await ws_routes.post_decision(req)
    assert resp.status == 400


# ───────────────────────── rejection still records cleanly ─────────────────────────


@pytest.mark.asyncio
async def test_reject_soul_proposal_does_not_touch_soul_md(
    isolated_home: Path,
) -> None:
    _seed_soul()
    store = EventStore(isolated_home / "logs")
    ev = store.append_event(WorkspaceEvent.new(
        kind="soul_proposal",
        source="feedback_consolidator",
        title="bullet",
        summary="x",
        payload={"action": "propose_soul_growth", "bullet": "Should not appear."},
    ))

    req = _StubReq(store, match={"event_id": ev.event_id}, body={"decision": "reject"})
    resp = await ws_routes.post_decision(req)
    assert resp.status == 200
    data = _resp_json(resp)
    assert data["status"] == "rejected"
    from tesseract.paths import workspace_dir
    soul_text = (workspace_dir() / "SOUL.md").read_text(encoding="utf-8")
    assert "Should not appear." not in soul_text
