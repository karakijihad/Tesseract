"""2026-07-03 TARS-routing map hygiene — three operator-reported fixes.

1. Boot rebuild seeds only recent controller sessions (382 idle rows /
   "+377 older" flooded the map; sessions never reach status "closed").
2. Config unreadable → rebuild fails OPEN (seeds everything, logs loudly) —
   boot resilience beats filtering.
3. Named ensure closes + archives the lane it replaces (a stale headless
   lane dir stays sendable forever and resurrects as a ghost map row).
Also pins the two cockpit.yaml keys backing the fixes.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from tesseract.orchestrator.activity.registry import (
    get_activity_registry,
    reset_activity_registry,
)


def _make_session(sid: str, *, last_active_hours_ago: float) -> None:
    from tesseract.orchestrator.tars_controller.sessions import (
        SessionRegistry,
        session_record_path,
    )

    SessionRegistry().create_session(
        mode="chat", origin="mirror", title=f"t-{sid}", session_id=sid
    )
    path = session_record_path(sid)
    payload = json.loads(path.read_text(encoding="utf-8"))
    stamp = datetime.now(timezone.utc) - timedelta(hours=last_active_hours_ago)
    payload["last_active_at"] = stamp.isoformat()
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_rebuild_skips_sessions_older_than_window(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    from tesseract.orchestrator.activity.rebuild import rebuild_from_disk

    _make_session("2026-07-03-aaaa0001", last_active_hours_ago=1)
    _make_session("2026-06-01-bbbb0002", last_active_hours_ago=24 * 30)
    # create_session registers live records itself — reset so only the
    # boot-time rebuild path populates the registry under test.
    reset_activity_registry()

    n = rebuild_from_disk()
    reg = get_activity_registry()
    assert reg.get("session:2026-07-03-aaaa0001") is not None
    assert reg.get("session:2026-06-01-bbbb0002") is None
    assert n == 1


def test_rebuild_fails_open_when_window_config_unreadable(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    import tesseract.config.cockpit as cockpit_cfg
    from tesseract.orchestrator.activity.rebuild import rebuild_from_disk

    def _boom() -> float:
        raise KeyError("activity")

    monkeypatch.setattr(
        cockpit_cfg, "load_activity_rebuild_window_hours", _boom
    )
    _make_session("2026-05-01-cccc0003", last_active_hours_ago=24 * 60)
    reset_activity_registry()

    rebuild_from_disk()
    assert get_activity_registry().get("session:2026-05-01-cccc0003") is not None


def test_cockpit_yaml_carries_the_two_tuned_keys() -> None:
    from tesseract.config.cockpit import (
        load_activity_rebuild_window_hours,
        load_conductor_reply_cap,
    )

    assert load_activity_rebuild_window_hours() == 48.0
    # 8000 truncated a plain MCP-inventory reply → slice-loop token burn.
    assert load_conductor_reply_cap() >= 24000


class _FakeLaneManager:
    def __init__(self) -> None:
        self.closed: list[tuple[str, str]] = []
        self.opened = 0

    async def open(self, **_kw) -> str:
        self.opened += 1
        return f"lane-claude-new{self.opened}"

    async def close(self, lane_id: str, reason: str) -> dict:
        self.closed.append((lane_id, reason))
        return {"final_status": "closed"}


def test_ensure_closes_replaced_dead_lane(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_activity_registry()
    from tesseract.orchestrator.tars_controller.lanes.named import (
        NamedLaneManager,
        NamedLaneRecord,
        read_named_lane,
        write_named_lane,
    )

    # Binding points at a lane with no lane.json on disk → judged dead.
    write_named_lane(
        NamedLaneRecord(
            name="coder/claude",
            lane_id="lane-claude-ghost",
            kind="claude",
            model="test-model",
            working_dir=str(tmp_path),
        )
    )
    fake = _FakeLaneManager()
    mgr = NamedLaneManager(lane_manager=fake)  # type: ignore[arg-type]

    record = asyncio.run(
        mgr.ensure(
            "coder/claude",
            kind="claude",
            model="test-model",
            working_dir=str(tmp_path),
        )
    )

    assert fake.closed == [("lane-claude-ghost", "replaced by named ensure")]
    assert record.lane_id == "lane-claude-new1"
    assert read_named_lane("coder/claude").lane_id == "lane-claude-new1"


def test_ensure_fresh_name_closes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_activity_registry()
    from tesseract.orchestrator.tars_controller.lanes.named import NamedLaneManager

    fake = _FakeLaneManager()
    mgr = NamedLaneManager(lane_manager=fake)  # type: ignore[arg-type]
    asyncio.run(
        mgr.ensure(
            "auditor/codex",
            kind="codex",
            model="test-model",
            working_dir=str(tmp_path),
        )
    )
    assert fake.closed == []


def test_ensure_close_failure_does_not_block_replacement(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_activity_registry()
    from tesseract.orchestrator.tars_controller.lanes.named import (
        NamedLaneManager,
        NamedLaneRecord,
        write_named_lane,
    )

    write_named_lane(
        NamedLaneRecord(
            name="coder/claude",
            lane_id="lane-claude-ghost",
            kind="claude",
            model="test-model",
            working_dir=str(tmp_path),
        )
    )

    class _ExplodingClose(_FakeLaneManager):
        async def close(self, lane_id: str, reason: str) -> dict:
            raise RuntimeError("archive dir locked")

    fake = _ExplodingClose()
    mgr = NamedLaneManager(lane_manager=fake)  # type: ignore[arg-type]
    record = asyncio.run(
        mgr.ensure(
            "coder/claude",
            kind="claude",
            model="test-model",
            working_dir=str(tmp_path),
        )
    )
    assert record.lane_id == "lane-claude-new1"
