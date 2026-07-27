"""M2 regression - ChatDigestJob must not drop or misattribute cross-midnight sessions."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ChunkType,
    ModelAdapter,
    StreamChunk,
)
from tesseract.scheduler.tasks.chat_digest import (
    ChatDigestJob,
    _build_transcript,
    _collect_sessions,
)
from tesseract.scheduler.types import JobContext


def _write_session(session_dir: Path, name: str, started_at: str, ended_at: str) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "started_at": started_at,
        "ended_at": ended_at,
        "turn_count": 1,
        "model": "stub",
        "history": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ],
    }
    (session_dir / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


class _CapturingAdapter(ModelAdapter):
    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    async def generate(self, prompt: str, options: AdapterOptions | None = None) -> str:
        self.prompts.append(prompt)
        return self.response

    async def stream(
        self,
        messages,
        tools=None,
        options=None,
    ) -> AsyncGenerator[StreamChunk, None]:
        yield StreamChunk(type=ChunkType.TEXT, text=self.response)

    def count_tokens(self, messages) -> int:
        return 0

    async def check_available(self) -> bool:
        return True


def test_cross_midnight_session_lands_in_start_day_digest(tmp_path) -> None:
    session_dir = tmp_path / "sessions"
    _write_session(
        session_dir,
        name="overnight",
        started_at="2026-04-22T23:30:00+00:00",
        ended_at="2026-04-23T00:10:00+00:00",
    )

    sessions = _collect_sessions(date(2026, 4, 22), session_dir)

    assert len(sessions) == 1, (
        f"session that started on target day must be included; got {sessions}"
    )


def test_cross_midnight_session_lands_in_end_day_digest(tmp_path) -> None:
    session_dir = tmp_path / "sessions"
    _write_session(
        session_dir,
        name="overnight",
        started_at="2026-04-22T23:30:00+00:00",
        ended_at="2026-04-23T00:10:00+00:00",
    )

    sessions = _collect_sessions(date(2026, 4, 23), session_dir)

    assert len(sessions) == 1, (
        f"session that ended on target day must also be included; got {sessions}"
    )


def test_session_outside_target_day_excluded(tmp_path) -> None:
    session_dir = tmp_path / "sessions"
    _write_session(
        session_dir,
        name="yesterday",
        started_at="2026-04-21T10:00:00+00:00",
        ended_at="2026-04-21T11:00:00+00:00",
    )

    sessions = _collect_sessions(date(2026, 4, 22), session_dir)

    assert sessions == [], "unrelated sessions must not leak into the digest"


def test_build_transcript_filters_timestamped_turns_to_target_day() -> None:
    session = type(
        "_Session",
        (),
        {
            "started_at": "2026-04-22T23:50:00+00:00",
            "ended_at": "2026-04-23T00:05:00+00:00",
            "history": [
                {
                    "role": "user",
                    "content": "late-night plan",
                    "timestamp": "2026-04-22T23:50:00+00:00",
                },
                {
                    "role": "assistant",
                    "content": "continue tomorrow",
                    "timestamp": "2026-04-23T00:05:00+00:00",
                },
            ],
        },
    )()

    start_day = _build_transcript([session], max_chars=500, target=date(2026, 4, 22))
    end_day = _build_transcript([session], max_chars=500, target=date(2026, 4, 23))

    assert "USER: late-night plan" in start_day
    assert "ASSISTANT: continue tomorrow" not in start_day
    assert "ASSISTANT: continue tomorrow" in end_day
    assert "USER: late-night plan" not in end_day


async def test_chat_digest_uses_message_timestamps_for_cross_midnight_sessions(tmp_path) -> None:
    session_dir = tmp_path / "sessions"
    daily_dir = tmp_path / "daily"
    _write_session(
        session_dir,
        name="overnight",
        started_at="2026-04-22T23:50:00+00:00",
        ended_at="2026-04-23T00:10:00+00:00",
    )

    path = session_dir / "overnight.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["history"] = [
        {
            "role": "user",
            "content": "late-night plan",
            "timestamp": "2026-04-22T23:50:00+00:00",
        },
        {
            "role": "assistant",
            "content": "continue tomorrow",
            "timestamp": "2026-04-23T00:05:00+00:00",
        },
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")

    adapter = _CapturingAdapter(
        "Digest body that is long enough to be written to disk safely.",
    )
    opts = AdapterOptions()
    ctx = JobContext(
        job_name="chat_digest",
        fired_at=datetime(2026, 4, 23, 23, 50, tzinfo=timezone.utc),
        app={
            "adapter": adapter,
            "adapter_options": opts,
            # `_resolve_adapter_chain` prefers `adapter_chain` over the
            # legacy singleton; without this it builds a real chain from
            # `roles.yaml` and bypasses the test stub.
            "adapter_chain": [(adapter, opts)],
        },
        config={
            "session_dir": str(session_dir),
            "daily_dir": str(daily_dir),
            "max_digest_chars": 1000,
        },
    )

    result = await ChatDigestJob().run(ctx)

    assert result.ok is True, result.detail
    assert len(adapter.prompts) == 1
    prompt = adapter.prompts[0]
    assert "USER: late-night plan" in prompt
    assert "ASSISTANT: continue tomorrow" not in prompt
