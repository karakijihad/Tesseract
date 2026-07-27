"""Regression suite for memory-retune M3 — ChatDigestJob.

Covers:
  * Empty sessions directory → ok=True, wrote=False, no daily file created.
  * Sessions from yesterday → single LLM call, `[chat_digest]` section
    appended to the correct daily file with the digest body.
  * Second fire on the same day is idempotent (wrote=False, one header).
  * Tool-role turns and `_reasoning: True` blobs are stripped from the
    prompt passed to the adapter.

Contract: `Docs/Plan/memory-retune/_shared/memory-stream-contract.md`.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncGenerator

from tesseract.brain.session_store import SessionState
from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ChunkType,
    ModelAdapter,
    StreamChunk,
)
from tesseract.scheduler.tasks.chat_digest import ChatDigestJob
from tesseract.scheduler.types import JobContext


# ── Helpers ───────────────────────────────────────────────────────────────


NOW = datetime(2026, 4, 23, 23, 50, tzinfo=timezone.utc)
YESTERDAY = (NOW - timedelta(days=1)).date()
YESTERDAY_STEM = YESTERDAY.isoformat()


class _CapturingAdapter(ModelAdapter):
    """ModelAdapter stub that records every `generate()` call.

    Only `generate()` is exercised by ChatDigestJob; the rest of the ABC
    is stubbed to keep instantiation legal.
    """

    def __init__(self, response: str) -> None:
        self._response = response
        self.call_count = 0
        self.last_prompt = ""

    async def generate(self, prompt: str, options: AdapterOptions | None = None) -> str:
        self.call_count += 1
        self.last_prompt = prompt
        return self._response

    async def stream(
        self,
        messages,
        tools=None,
        options=None,
    ) -> AsyncGenerator[StreamChunk, None]:
        yield StreamChunk(type=ChunkType.TEXT, text=self._response)

    def count_tokens(self, messages) -> int:
        return 0

    async def check_available(self) -> bool:
        return True


def _write_session(
    session_dir: Path,
    name: str,
    history: list[dict],
    *,
    ended_at: str,
    started_at: str = "2026-04-22T09:00:00+00:00",
) -> Path:
    session_dir.mkdir(parents=True, exist_ok=True)
    state = SessionState(
        started_at=started_at,
        ended_at=ended_at,
        turn_count=sum(1 for m in history if m.get("role") == "user"),
        model="gpt-5.4-nano",
        history=history,
    )
    path = session_dir / f"{name}.json"
    path.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
    return path


def _yesterday_ended_at() -> str:
    """ISO timestamp that falls on YESTERDAY when converted to UTC."""
    return f"{YESTERDAY_STEM}T20:00:00+00:00"


def _ctx(tmp_path: Path, *, adapter: ModelAdapter | None) -> JobContext:
    app: dict = {}
    if adapter is not None:
        opts = AdapterOptions()
        app["adapter"] = adapter
        app["adapter_options"] = opts
        # `_resolve_adapter_chain` prefers `app["adapter_chain"]` over the
        # legacy singleton; without this it falls back to building a real
        # chain from `roles.yaml`, which bypasses the test stub.
        app["adapter_chain"] = [(adapter, opts)]
    return JobContext(
        job_name="chat_digest",
        fired_at=NOW,
        app=app or None,
        config={
            "session_dir": str(tmp_path / "sessions"),
            "daily_dir": str(tmp_path / "daily"),
            "max_digest_chars": 6000,
        },
    )


# ── Tests ─────────────────────────────────────────────────────────────────


async def test_chat_digest_job_no_sessions(tmp_path: Path) -> None:
    adapter = _CapturingAdapter(response="should not be called")
    ctx = _ctx(tmp_path, adapter=adapter)

    result = await ChatDigestJob().run(ctx)

    assert result.ok is True
    assert result.payload["sessions"] == 0
    assert result.payload["wrote"] is False
    assert adapter.call_count == 0
    assert not (tmp_path / "daily" / f"{YESTERDAY_STEM}.md").exists()


async def test_chat_digest_job_writes_section(tmp_path: Path) -> None:
    adapter = _CapturingAdapter(
        response=(
            "Operator walked through the M3 plan and confirmed the nightly "
            "digest cadence. Decided to route into daily/ alongside the M2 "
            "prefix router. Learned that transcripts were not being captured. "
            "Deferred Mirror digest UI to Phase 15."
        ),
    )
    _write_session(
        tmp_path / "sessions",
        name="2026-04-22-1000",
        history=[
            {"role": "user", "content": "What should we tackle tonight?"},
            {"role": "assistant", "content": "M3 chat digest — I'll wire it now."},
        ],
        ended_at=_yesterday_ended_at(),
    )
    _write_session(
        tmp_path / "sessions",
        name="2026-04-22-2000",
        history=[
            {"role": "user", "content": "Cadence confirmed?"},
            {"role": "assistant", "content": "23:50 UTC, idempotent on re-run."},
        ],
        ended_at=_yesterday_ended_at(),
    )
    ctx = _ctx(tmp_path, adapter=adapter)

    result = await ChatDigestJob().run(ctx)

    assert result.ok is True, result.detail
    assert result.payload["sessions"] == 2
    assert result.payload["wrote"] is True
    assert adapter.call_count == 1

    daily_file = tmp_path / "daily" / f"{YESTERDAY_STEM}.md"
    assert daily_file.exists()
    text = daily_file.read_text(encoding="utf-8")
    assert f"## [chat_digest] {YESTERDAY_STEM}" in text
    assert "Operator walked through the M3 plan" in text


async def test_chat_digest_idempotent(tmp_path: Path) -> None:
    adapter = _CapturingAdapter(
        response=(
            "Short digest body that clearly exceeds the librarian's 80-character "
            "minimum section floor for safe promotion."
        ),
    )
    _write_session(
        tmp_path / "sessions",
        name="2026-04-22-1500",
        history=[
            {"role": "user", "content": "First message."},
            {"role": "assistant", "content": "First reply."},
        ],
        ended_at=_yesterday_ended_at(),
    )
    ctx = _ctx(tmp_path, adapter=adapter)

    first = await ChatDigestJob().run(ctx)
    assert first.payload["wrote"] is True

    second = await ChatDigestJob().run(ctx)
    assert second.ok is True
    assert second.payload["wrote"] is False

    daily_file = tmp_path / "daily" / f"{YESTERDAY_STEM}.md"
    text = daily_file.read_text(encoding="utf-8")
    header = f"## [chat_digest] {YESTERDAY_STEM}"
    assert text.count(header) == 1


async def test_chat_digest_reports_failure_when_chain_exhausted(tmp_path: Path) -> None:
    """All chain members failing must surface as ok=False so the scheduler's
    retry policy engages — silent ok=True was the pre-fix pathology."""

    class _FailingAdapter(_CapturingAdapter):
        async def generate(self, prompt: str, options: AdapterOptions | None = None) -> str:
            self.call_count += 1
            self.last_prompt = prompt
            raise RuntimeError("adapter down (simulated)")

    primary = _FailingAdapter(response="unused")
    secondary = _FailingAdapter(response="unused")
    _write_session(
        tmp_path / "sessions",
        name="2026-04-22-1300",
        history=[
            {"role": "user", "content": "Digest please."},
            {"role": "assistant", "content": "Working."},
        ],
        ended_at=_yesterday_ended_at(),
    )

    app: dict = {
        "adapter_chain": [
            (primary, AdapterOptions()),
            (secondary, AdapterOptions()),
        ],
    }
    ctx = JobContext(
        job_name="chat_digest",
        fired_at=NOW,
        app=app,
        config={
            "session_dir": str(tmp_path / "sessions"),
            "daily_dir": str(tmp_path / "daily"),
            "max_digest_chars": 6000,
        },
    )

    result = await ChatDigestJob().run(ctx)

    assert result.ok is False
    assert result.payload["wrote"] is False
    assert primary.call_count == 1
    assert secondary.call_count == 1
    assert not (tmp_path / "daily" / f"{YESTERDAY_STEM}.md").exists()


async def test_chat_digest_falls_back_when_primary_errors(tmp_path: Path) -> None:
    """Chain wiring: primary adapter.generate() raising must not silently skip
    the digest — the job must hop to the next chain member."""

    class _FailingAdapter(_CapturingAdapter):
        async def generate(self, prompt: str, options: AdapterOptions | None = None) -> str:
            self.call_count += 1
            self.last_prompt = prompt
            raise RuntimeError("primary unavailable (simulated)")

    primary = _FailingAdapter(response="unused")
    secondary = _CapturingAdapter(
        response="Secondary digest body that clears the 80-char floor for safe librarian promotion.",
    )
    _write_session(
        tmp_path / "sessions",
        name="2026-04-22-1200",
        history=[
            {"role": "user", "content": "Summarize this."},
            {"role": "assistant", "content": "On it."},
        ],
        ended_at=_yesterday_ended_at(),
    )

    app: dict = {
        "adapter_chain": [
            (primary, AdapterOptions()),
            (secondary, AdapterOptions()),
        ],
    }
    ctx = JobContext(
        job_name="chat_digest",
        fired_at=NOW,
        app=app,
        config={
            "session_dir": str(tmp_path / "sessions"),
            "daily_dir": str(tmp_path / "daily"),
            "max_digest_chars": 6000,
        },
    )

    result = await ChatDigestJob().run(ctx)

    assert result.ok is True, result.detail
    assert result.payload["wrote"] is True
    assert primary.call_count == 1
    assert secondary.call_count == 1

    daily_file = tmp_path / "daily" / f"{YESTERDAY_STEM}.md"
    assert daily_file.exists()
    text = daily_file.read_text(encoding="utf-8")
    assert "Secondary digest body" in text


async def test_chat_digest_strips_tool_turns(tmp_path: Path) -> None:
    adapter = _CapturingAdapter(
        response="Digest body long enough to clear the librarian's 80-char floor for safe promotion.",
    )
    _write_session(
        tmp_path / "sessions",
        name="2026-04-22-1800",
        history=[
            {"role": "user", "content": "Keep this user line."},
            {"_reasoning": True, "type": "reasoning", "encrypted_content": "SECRET_REASONING_BLOB"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "memory_search", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "TOOL_OUTPUT_SHOULD_BE_STRIPPED"},
            {"role": "assistant", "content": "Keep this assistant reply."},
        ],
        ended_at=_yesterday_ended_at(),
    )
    ctx = _ctx(tmp_path, adapter=adapter)

    result = await ChatDigestJob().run(ctx)

    assert result.ok is True, result.detail
    assert adapter.call_count == 1
    prompt = adapter.last_prompt

    assert "Keep this user line." in prompt
    assert "Keep this assistant reply." in prompt
    assert "TOOL:" not in prompt
    assert "TOOL_OUTPUT_SHOULD_BE_STRIPPED" not in prompt
    assert "SECRET_REASONING_BLOB" not in prompt
