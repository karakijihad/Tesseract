"""TranscriptWriter + TranscriptReader round-trip, replay, and tail."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tesseract.orchestrator.tars_controller import (
    AssistantTextEvent,
    ArtifactEvent,
    ChildTranscriptRefEvent,
    GenericTranscriptEvent,
    JournalEntryEvent,
    PermissionRequestEvent,
    PtyChunkEvent,
    ToolResultEvent,
    ToolUseEvent,
    TranscriptReader,
    TranscriptWriter,
    UserTextEvent,
    WorkerStatusEvent,
    mint_session_id,
    parse_event,
    transcript_path,
)


def test_append_writes_jsonl(isolated_home: Path) -> None:
    sid = mint_session_id()
    writer = TranscriptWriter(sid)
    evt = UserTextEvent(session_id=sid, origin="cli", text="hello")
    offset = writer.append(evt)
    assert offset > 0
    raw = transcript_path(sid).read_bytes()
    assert raw.endswith(b"\n")
    line = raw.decode("utf-8").strip()
    payload = json.loads(line)
    assert payload["kind"] == "user_text"
    assert payload["text"] == "hello"
    assert payload["session_id"] == sid


def test_all_baseline_event_kinds_round_trip(isolated_home: Path) -> None:
    sid = mint_session_id()
    writer = TranscriptWriter(sid)
    events = [
        UserTextEvent(session_id=sid, origin="cli", text="hi"),
        AssistantTextEvent(session_id=sid, origin="chat", text="hello"),
        ToolUseEvent(
            session_id=sid,
            origin="chat",
            tool="delegate_tars_controller",
            input={"task": "do thing"},
            tool_use_id="tu-1",
        ),
        ToolResultEvent(
            session_id=sid,
            origin="chat",
            tool_use_id="tu-1",
            success=True,
            output={"out": "done"},
        ),
        PermissionRequestEvent(
            session_id=sid,
            origin="chat",
            tool="bash",
            summary="rm /tmp/x",
            posture="ask",
        ),
        WorkerStatusEvent(
            session_id=sid,
            origin="autonomy",
            worker_id="w-1",
            worker_kind="claude_cli",
            status="running",
        ),
        ArtifactEvent(
            session_id=sid,
            origin="autonomy",
            artifact_type="file",
            path="/abs/file.txt",
        ),
        ChildTranscriptRefEvent(
            session_id=sid,
            origin="autonomy",
            child_session_id="2026-05-23-aaaaaaaa",
            child_transcript_path="/abs/child.jsonl",
        ),
        JournalEntryEvent(
            session_id=sid,
            origin="autonomy",
            entry_type="dispatch",
            summary="dispatched",
        ),
        PtyChunkEvent(
            session_id=sid,
            origin="cli",
            worker_id="w-1",
            data_b64="aGVsbG8=",
        ),
    ]
    for evt in events:
        writer.append(evt)

    reader = TranscriptReader(sid)
    replayed = [evt for evt, _off in reader.read_from(0)]
    assert len(replayed) == len(events)
    for original, parsed in zip(events, replayed):
        assert type(parsed) is type(original)
        assert parsed.kind == original.kind
        assert parsed.session_id == sid


def test_unknown_kind_round_trips_via_generic(isolated_home: Path) -> None:
    sid = mint_session_id()
    writer = TranscriptWriter(sid)
    payload = {
        "event_id": "evt-aaaa",
        "session_id": sid,
        "ts": "2026-05-23T00:00:00.000Z",
        "kind": "future_extension_kind",
        "origin": "cli",
        "extra_field": {"nested": [1, 2, 3]},
        "another": "preserved",
    }
    writer.append(payload)

    reader = TranscriptReader(sid)
    parsed = list(reader.read_from(0))
    assert len(parsed) == 1
    evt, _off = parsed[0]
    assert isinstance(evt, GenericTranscriptEvent)
    assert evt.kind == "future_extension_kind"
    dumped = evt.model_dump(mode="json")
    assert dumped["extra_field"] == {"nested": [1, 2, 3]}
    assert dumped["another"] == "preserved"


def test_read_from_offset_resumes_correctly(isolated_home: Path) -> None:
    sid = mint_session_id()
    writer = TranscriptWriter(sid)
    writer.append(UserTextEvent(session_id=sid, origin="cli", text="one"))
    writer.append(UserTextEvent(session_id=sid, origin="cli", text="two"))
    mid_offset = writer.append(UserTextEvent(session_id=sid, origin="cli", text="three"))
    writer.append(UserTextEvent(session_id=sid, origin="cli", text="four"))

    reader = TranscriptReader(sid)
    after = [evt.text for evt, _off in reader.read_from(mid_offset)]
    assert after == ["four"]


def test_corrupt_lines_are_skipped(isolated_home: Path) -> None:
    sid = mint_session_id()
    writer = TranscriptWriter(sid)
    writer.append(UserTextEvent(session_id=sid, origin="cli", text="ok"))
    path = transcript_path(sid)
    with path.open("ab") as fh:
        fh.write(b"{not valid json\n")
        fh.write(b"\n")
    writer.append(UserTextEvent(session_id=sid, origin="cli", text="after"))

    reader = TranscriptReader(sid)
    texts = [evt.text for evt, _off in reader.read_from(0)]
    assert texts == ["ok", "after"]


def test_read_from_missing_file_yields_nothing(isolated_home: Path) -> None:
    sid = mint_session_id()
    reader = TranscriptReader(sid)
    assert list(reader.read_from(0)) == []


@pytest.mark.asyncio
async def test_tail_yields_events_appended_after_start(isolated_home: Path) -> None:
    sid = mint_session_id()
    writer = TranscriptWriter(sid)
    # Seed one row so the file exists.
    writer.append(UserTextEvent(session_id=sid, origin="cli", text="seed"))

    reader = TranscriptReader(sid)
    stop = asyncio.Event()
    collected: list[str] = []

    async def consumer() -> None:
        async for evt, _off in reader.tail(from_offset=0, poll_interval=0.01, stop_event=stop):
            assert hasattr(evt, "text")
            collected.append(evt.text)  # type: ignore[union-attr]
            if len(collected) >= 3:
                stop.set()
                return

    async def producer() -> None:
        await asyncio.sleep(0.05)
        writer.append(UserTextEvent(session_id=sid, origin="cli", text="live-1"))
        await asyncio.sleep(0.05)
        writer.append(UserTextEvent(session_id=sid, origin="cli", text="live-2"))

    await asyncio.wait_for(asyncio.gather(consumer(), producer()), timeout=5.0)
    assert collected == ["seed", "live-1", "live-2"]


def test_parse_event_helper_dispatches_by_kind() -> None:
    payload = {
        "event_id": "evt-x",
        "session_id": "2026-05-23-aaaaaaaa",
        "ts": "2026-05-23T00:00:00.000Z",
        "kind": "tool_use",
        "origin": "chat",
        "tool": "bash",
        "tool_use_id": "tu-1",
        "input": {"command": "echo hi"},
    }
    evt = parse_event(payload)
    assert isinstance(evt, ToolUseEvent)
    assert evt.tool == "bash"


def test_append_accepts_plain_dict(isolated_home: Path) -> None:
    sid = mint_session_id()
    writer = TranscriptWriter(sid)
    writer.append(
        {
            "event_id": "evt-1",
            "session_id": sid,
            "ts": "2026-05-23T00:00:00.000Z",
            "kind": "user_text",
            "origin": "cli",
            "text": "plain dict",
        }
    )
    reader = TranscriptReader(sid)
    rows = list(reader.read_from(0))
    assert len(rows) == 1
    evt, _ = rows[0]
    assert isinstance(evt, UserTextEvent)
    assert evt.text == "plain dict"
