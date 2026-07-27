"""X-4 Session B — `transcript.txt` + `last_cursor.txt` writes.

Per `_shared/lane-contract.md §Persistence` the four files under a
lane's directory are:
- `lane.json` (atomic write — already covered by store tests)
- `events.jsonl` (already covered by events_log tests)
- `transcript.txt` (Session B addition — model-side rendered prose)
- `last_cursor.txt` (Session B addition — advisory last-cursor written
  on every read)"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from tesseract.orchestrator.tars_controller.lanes import Lane, LaneManager
from tesseract.orchestrator.tars_controller.lanes.manager import LaneRuntime


class _StubAdapter:
    async def run_turn(
        self,
        *,
        message: str,
        on_event: Callable[[dict[str, Any]], None],
        cancel_event: asyncio.Event | None,
    ) -> dict[str, Any]:
        on_event({"type": "system", "subtype": "init", "session_id": "sess-tx"})
        on_event({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Let me check the file."},
                    {
                        "type": "tool_use",
                        "id": "tu-42",
                        "name": "Read",
                        "input": {"path": "/etc/hosts"},
                    },
                ],
            },
        })
        on_event({
            "type": "user",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "tu-42",
                    "content": "127.0.0.1 localhost",
                    "is_error": False,
                }],
            },
        })
        on_event({"type": "result", "subtype": "success", "result": "", "usage": {}})
        return {"session_id": "sess-tx", "is_error": False, "usage": {}}


def _stub_factory(lane: Lane, runtime: LaneRuntime) -> Any:
    return _StubAdapter()


def test_transcript_txt_captures_assistant_and_tool_prose(
    isolated_home: Path,
) -> None:
    mgr = LaneManager(adapter_factory=_stub_factory)
    lane_id = asyncio.run(
        mgr.open(
            kind="claude",
            mode="headless",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    asyncio.run(mgr.send(lane_id, "inspect hosts"))

    transcript_path = isolated_home / "controller" / "lanes" / lane_id / "transcript.txt"
    assert transcript_path.exists()
    text = transcript_path.read_text(encoding="utf-8")
    assert "assistant: Let me check the file." in text
    assert "tool_use[tu-42] Read:" in text
    assert "tool_result[tu-42]:" in text
    assert "127.0.0.1 localhost" in text
    assert "status_change" not in text
    assert "turn_started" not in text
    assert "turn_ended" not in text


def test_last_cursor_txt_updated_on_every_read(
    isolated_home: Path,
) -> None:
    mgr = LaneManager(adapter_factory=_stub_factory)
    lane_id = asyncio.run(
        mgr.open(
            kind="claude",
            mode="headless",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    asyncio.run(mgr.send(lane_id, "hi"))

    _, cursor_a = mgr.read(lane_id, None)
    cursor_file = isolated_home / "controller" / "lanes" / lane_id / "last_cursor.txt"
    assert cursor_file.exists()
    assert cursor_file.read_text(encoding="utf-8") == cursor_a

    asyncio.run(mgr.send(lane_id, "again"))
    _, cursor_b = mgr.read(lane_id, cursor_a)
    assert cursor_file.read_text(encoding="utf-8") == cursor_b
    assert int(cursor_b) >= int(cursor_a)


def test_transcript_closed_event_recorded(isolated_home: Path) -> None:
    mgr = LaneManager(adapter_factory=_stub_factory)
    lane_id = asyncio.run(
        mgr.open(
            kind="claude",
            mode="headless",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    asyncio.run(mgr.send(lane_id, "hi"))
    result = asyncio.run(mgr.close(lane_id, "mission_complete"))
    archive_dir = Path(result["archive_dir"])
    transcript = (archive_dir / "transcript.txt").read_text(encoding="utf-8")
    assert "closed: mission_complete" in transcript
