"""Layer C — daily feedback sweep job.

Mocks the adapter chain so the test never makes a network call. Verifies:
- proposal JSONL is written to the resolved log dir
- WS envelope is broadcast to live sessions when present
- no memory mutation happens (read-only contract)
- empty / no-proposals path is benign
- adapter unavailability returns ok with skipped detail
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from tesseract.memory.types import MemoryFrontmatter, MemoryType, Stability
from tesseract.scheduler.tasks import feedback_sweep as fs
from tesseract.scheduler.tasks.feedback_sweep import FeedbackSweepJob
from tesseract.scheduler.types import JobContext


def _write_session(sessions_dir: Path, session_id: str, target_date, history: list) -> None:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc).isoformat()
    ended = (datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
             + timedelta(hours=1)).isoformat()
    payload = {
        "session_id": session_id,
        "started_at": started,
        "ended_at": ended,
        "model": "test/model",
        "history": history,
    }
    (sessions_dir / f"{session_id}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _ctx(tmp_path: Path, app=None) -> JobContext:
    fired = datetime(2026, 5, 5, 21, 0, tzinfo=timezone.utc)
    return JobContext(
        job_name="feedback_sweep",
        run_id="run-1",
        fired_at=fired,
        app=app,
        config={
            "session_dir": str(tmp_path / "sessions"),
            "store_dir": str(tmp_path / "memory-store"),
            "log_dir": str(tmp_path / "logs" / "feedback-sweep"),
        },
    )


def _seed_existing_feedback(store_dir: Path) -> None:
    target = store_dir / "feedback" / "mem_existing.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    fm = MemoryFrontmatter(
        id="mem_existing",
        type=MemoryType.FEEDBACK,
        title="Existing rule",
        summary="Already saved",
        importance=8,
        created_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        stability=Stability.ACTIVE,
    )
    text = "---\n" + yaml.dump(fm.to_yaml_dict(), sort_keys=False) + "---\n\nbody"
    target.write_text(text, encoding="utf-8")


@pytest.mark.asyncio
async def test_feedback_sweep_writes_jsonl_and_broadcasts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_date = datetime(2026, 5, 4, tzinfo=timezone.utc).date()
    _write_session(tmp_path / "sessions", "sess-1", target_date, [
        {"role": "user", "content": "from now on always cite the source file when quoting code"},
        {"role": "assistant", "content": "noted"},
    ])
    _seed_existing_feedback(tmp_path / "memory-store")

    proposals_json = json.dumps({"proposals": [
        {"title": "Cite source files", "summary": "Always include file path",
         "importance": 8, "source_quote": "always cite the source file"},
    ]})

    async def fake_call(prompt, chain, timeout):
        return proposals_json

    monkeypatch.setattr(fs, "_call_with_fallback", fake_call)
    monkeypatch.setattr("tesseract.scheduler.tasks.feedback_sweep.build_chain_for_job", lambda *a, **k: [("stub-adapter", None)])

    sent: list = []

    class _StubSession:
        session_id = "ws-1"

    async def fake_send(sess, env):
        sent.append(env)

    # Patch the lazy import path used inside _broadcast.
    import tesseract.mirror.server.envelope as env_mod
    import tesseract.mirror.server.session as sess_mod
    monkeypatch.setattr(sess_mod, "send_envelope", fake_send)

    app = {"server_sessions": {"ws-1": _StubSession()}}
    ctx = _ctx(tmp_path, app=app)

    job = FeedbackSweepJob()
    result = await job.run(ctx)

    assert result.ok is True
    assert result.payload["proposals"] == 1
    assert result.payload["sessions"] == 1

    log_path = Path(result.payload["log_path"])
    assert log_path.exists()
    lines = [json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["title"] == "Cite source files"
    assert lines[0]["importance"] == 8
    assert lines[0]["target_date"] == target_date.isoformat()

    # No memory mutation — only the seeded record should exist.
    feedback_files = list((tmp_path / "memory-store" / "feedback").glob("*.md"))
    assert {p.name for p in feedback_files} == {"mem_existing.md"}

    # WS envelope reached the live session.
    assert len(sent) == 1
    assert sent[0]["type"] == "feedback_proposals"
    assert sent[0]["data"]["source"] == "feedback_sweep"


@pytest.mark.asyncio
async def test_feedback_sweep_no_sessions_is_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tesseract.scheduler.tasks.feedback_sweep.build_chain_for_job", lambda *a, **k: [("stub", None)])
    job = FeedbackSweepJob()
    result = await job.run(_ctx(tmp_path))
    assert result.ok is True
    assert result.payload["sessions"] == 0
    assert result.payload["proposals"] == 0
    assert "no sessions" in result.detail


@pytest.mark.asyncio
async def test_feedback_sweep_adapter_unavailable_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_date = datetime(2026, 5, 4, tzinfo=timezone.utc).date()
    _write_session(tmp_path / "sessions", "sess-1", target_date, [
        {"role": "user", "content": "always do X"},
        {"role": "assistant", "content": "ok"},
    ])
    monkeypatch.setattr("tesseract.scheduler.tasks.feedback_sweep.build_chain_for_job", lambda *a, **k: [])

    job = FeedbackSweepJob()
    result = await job.run(_ctx(tmp_path))
    assert result.ok is True
    assert result.payload["proposals"] == 0
    assert "unavailable" in result.detail


@pytest.mark.asyncio
async def test_feedback_sweep_empty_response_no_proposals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_date = datetime(2026, 5, 4, tzinfo=timezone.utc).date()
    _write_session(tmp_path / "sessions", "sess-1", target_date, [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ])

    async def fake_call(prompt, chain, timeout):
        return '{"proposals": []}'

    monkeypatch.setattr(fs, "_call_with_fallback", fake_call)
    monkeypatch.setattr("tesseract.scheduler.tasks.feedback_sweep.build_chain_for_job", lambda *a, **k: [("stub", None)])

    job = FeedbackSweepJob()
    result = await job.run(_ctx(tmp_path))
    assert result.ok is True
    assert result.payload["proposals"] == 0


def test_parse_proposals_tolerates_fenced_json() -> None:
    raw = (
        "Here are the candidates I found:\n"
        "```json\n"
        '{"proposals": [{"title": "X", "summary": "y", "importance": 7}]}\n'
        "```\n"
    )
    out = fs._parse_proposals(raw)
    assert len(out) == 1
    assert out[0]["title"] == "X"
    assert out[0]["importance"] == 7


def test_parse_proposals_clamps_importance() -> None:
    raw = '{"proposals": [{"title": "A", "summary": "s", "importance": 99}]}'
    out = fs._parse_proposals(raw)
    assert out[0]["importance"] == 10


def test_parse_proposals_drops_missing_fields() -> None:
    raw = '{"proposals": [{"title": "", "summary": "ok", "importance": 5}, {"title": "ok", "summary": "ok", "importance": 5}]}'
    out = fs._parse_proposals(raw)
    assert len(out) == 1
    assert out[0]["title"] == "ok"


def test_parse_proposals_handles_garbage() -> None:
    assert fs._parse_proposals("") == []
    assert fs._parse_proposals("nope, no JSON here") == []
    assert fs._parse_proposals("{not valid json}") == []


def test_parse_proposals_tolerates_braces_inside_strings() -> None:
    """A greedy `\\{.*\\}` regex anchors on the first stray `{` and silently
    drops valid proposals when prose contains template syntax. Balanced-brace
    extraction must skip braces inside quoted segments."""
    raw = (
        "Use this template: {key}: value when summarising. Then return:\n"
        '{"proposals": [{"title": "Cite source", "summary": "always include path",'
        ' "importance": 7}]}\n'
    )
    out = fs._parse_proposals(raw)
    assert len(out) == 1
    assert out[0]["title"] == "Cite source"


def test_extract_first_json_object_balances_nested() -> None:
    inner = '{"a": {"b": {"c": 1}}, "d": "}"}'
    assert fs._extract_first_json_object(f"prose {inner} trailing") == inner


def test_parse_proposals_drops_low_importance() -> None:
    """Prompt instructs the model to skip importance 1-4. Enforce on the
    parser side too — a disobedient model shouldn't bypass the noise floor."""
    raw = (
        '{"proposals": ['
        '{"title": "noise", "summary": "low", "importance": 3},'
        '{"title": "keep", "summary": "ok", "importance": 5}'
        ']}'
    )
    out = fs._parse_proposals(raw)
    assert len(out) == 1
    assert out[0]["title"] == "keep"


def test_write_jsonl_replaces_on_retry(tmp_path: Path) -> None:
    """A retry on the same target_date must not duplicate proposals — the
    file is written with `w` mode, so a second invocation replaces."""
    target_date = datetime(2026, 5, 4, tzinfo=timezone.utc).date()
    log_dir = tmp_path / "feedback-sweep"

    class _Sess:
        session_id = "s1"

    p1 = fs._write_jsonl(log_dir, target_date, [{"title": "a", "summary": "s", "importance": 7}], [_Sess()])
    p2 = fs._write_jsonl(log_dir, target_date, [{"title": "a", "summary": "s", "importance": 7}], [_Sess()])
    assert p1 == p2
    lines = p1.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
