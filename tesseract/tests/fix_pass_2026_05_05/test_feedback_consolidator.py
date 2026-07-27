"""Layer B — weekly feedback consolidator job.

Mocks the adapter chain so the test never makes a network call. Verifies:
- proposal JSONL is written (one row per proposal, segmented by `kind`)
- WS envelope is broadcast to live sessions when present
- no memory mutation happens (read-only contract)
- below-floor record count → benign skip
- adapter unavailability → ok with skipped detail
- malformed proposals are dropped silently
- the active-directives pool unions feedback + user records
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from tesseract.memory.types import MemoryFrontmatter, MemoryType, Stability
from tesseract.scheduler.tasks import feedback_consolidator as fc
from tesseract.scheduler.tasks.feedback_consolidator import FeedbackConsolidatorJob
from tesseract.scheduler.types import JobContext


def _ctx(tmp_path: Path, app=None) -> JobContext:
    fired = datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc)
    return JobContext(
        job_name="feedback_consolidator",
        run_id="run-1",
        fired_at=fired,
        app=app,
        config={
            "store_dir": str(tmp_path / "memory-store"),
            "log_dir": str(tmp_path / "logs" / "consolidator"),
        },
    )


def _seed(
    store_dir: Path,
    *,
    mem_id: str,
    title: str,
    summary: str = "body",
    importance: int = 7,
    mem_type: MemoryType = MemoryType.FEEDBACK,
    auto_links: list[str] | None = None,
) -> None:
    fm = MemoryFrontmatter(
        id=mem_id,
        type=mem_type,
        title=title,
        summary=summary,
        importance=importance,
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        stability=Stability.ACTIVE,
        auto_links=auto_links or [],
    )
    target = store_dir / mem_type.value / f"{mem_id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    text = (
        "---\n"
        + yaml.dump(fm.to_yaml_dict(), default_flow_style=False, sort_keys=False)
        + "---\n\n"
        + summary
    )
    target.write_text(text, encoding="utf-8")


@pytest.mark.asyncio
async def test_consolidator_writes_jsonl_and_broadcasts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_dir = tmp_path / "memory-store"
    _seed(store_dir, mem_id="mem_a", title="A rule", importance=8)
    _seed(store_dir, mem_id="mem_b", title="B rule", importance=7)
    _seed(store_dir, mem_id="mem_c", title="C rule", importance=6)

    proposals = json.dumps({
        "merges": [
            {"keep": "mem_a", "absorb": ["mem_b"], "reason": "same intent"},
        ],
        "soul": [
            {"bullet": "Operator wants tight, direct answers — no menus.",
             "supporting_ids": ["mem_a", "mem_b", "mem_c"]},
        ],
        "archives": [
            {"id": "mem_c", "reason": "superseded by mem_a"},
        ],
    })

    async def fake_call(prompt, chain, timeout):
        return proposals

    monkeypatch.setattr(fc, "_call_with_fallback", fake_call)
    monkeypatch.setattr("tesseract.scheduler.tasks.feedback_consolidator.build_chain_for_job", lambda *a, **k: [("stub", None)])

    sent: list = []

    class _StubSession:
        session_id = "ws-1"

    async def fake_send(sess, env):
        sent.append(env)

    import tesseract.mirror.server.session as sess_mod
    monkeypatch.setattr(sess_mod, "send_envelope", fake_send)

    app = {"server_sessions": {"ws-1": _StubSession()}}
    ctx = _ctx(tmp_path, app=app)

    job = FeedbackConsolidatorJob()
    result = await job.run(ctx)

    assert result.ok is True
    assert result.payload["records"] == 3
    assert result.payload["proposals"] == 3
    assert result.payload["kinds"] == {"merges": 1, "soul": 1, "archives": 1}

    log_path = Path(result.payload["log_path"])
    assert log_path.exists()
    rows = [json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines()]
    kinds = [r["kind"] for r in rows]
    assert sorted(kinds) == ["archives", "merges", "soul"]

    # No memory mutation — every seeded record still active.
    files = list((store_dir / "feedback").glob("*.md"))
    assert {p.name for p in files} == {"mem_a.md", "mem_b.md", "mem_c.md"}

    assert len(sent) == 1
    assert sent[0]["type"] == "feedback_proposals"
    assert sent[0]["data"]["source"] == "feedback_consolidator"


@pytest.mark.asyncio
async def test_consolidator_below_floor_is_benign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_dir = tmp_path / "memory-store"
    _seed(store_dir, mem_id="mem_only", title="solo", importance=8)
    monkeypatch.setattr("tesseract.scheduler.tasks.feedback_consolidator.build_chain_for_job", lambda *a, **k: [("stub", None)])

    job = FeedbackConsolidatorJob()
    result = await job.run(_ctx(tmp_path))
    assert result.ok is True
    assert result.payload["records"] == 1
    assert result.payload["proposals"] == 0
    assert "below floor" in result.detail


@pytest.mark.asyncio
async def test_consolidator_adapter_unavailable_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_dir = tmp_path / "memory-store"
    for n in ("a", "b", "c"):
        _seed(store_dir, mem_id=f"mem_{n}", title=n, importance=7)
    monkeypatch.setattr("tesseract.scheduler.tasks.feedback_consolidator.build_chain_for_job", lambda *a, **k: [])

    job = FeedbackConsolidatorJob()
    result = await job.run(_ctx(tmp_path))
    assert result.ok is True
    assert result.payload["proposals"] == 0
    assert "unavailable" in result.detail


@pytest.mark.asyncio
async def test_consolidator_pools_user_and_feedback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User-type rules drift the same way feedback does — the consolidator
    must consider both subdirs when proposing merges/archives."""
    store_dir = tmp_path / "memory-store"
    _seed(store_dir, mem_id="mem_fb1", title="fb1", importance=8)
    _seed(store_dir, mem_id="mem_fb2", title="fb2", importance=7)
    _seed(store_dir, mem_id="mem_user", title="inline preview default",
          importance=8, mem_type=MemoryType.USER)

    captured_prompt: list[str] = []

    async def fake_call(prompt, chain, timeout):
        captured_prompt.append(prompt)
        return '{"merges": [], "soul": [], "archives": []}'

    monkeypatch.setattr(fc, "_call_with_fallback", fake_call)
    monkeypatch.setattr("tesseract.scheduler.tasks.feedback_consolidator.build_chain_for_job", lambda *a, **k: [("stub", None)])

    job = FeedbackConsolidatorJob()
    result = await job.run(_ctx(tmp_path))
    assert result.ok is True
    assert result.payload["records"] == 3
    # The prompt body must include all three ids — operator's user-saved rule
    # shouldn't be invisible to the consolidator just because it sits under
    # `user/` instead of `feedback/`.
    prompt = captured_prompt[0]
    assert "mem_fb1" in prompt
    assert "mem_fb2" in prompt
    assert "mem_user" in prompt


def test_parse_proposals_drops_malformed() -> None:
    raw = json.dumps({
        "merges": [
            {"keep": "x", "absorb": []},                    # empty absorb → drop
            {"keep": "x", "absorb": ["x"]},                 # self-merge filtered → drop
            {"keep": "y", "absorb": ["z"], "reason": "ok"}, # valid
        ],
        "soul": [
            {"bullet": "too short ids", "supporting_ids": ["a", "b"]},  # <3 → drop
            {"bullet": "x" * 300, "supporting_ids": ["a", "b", "c"]},   # too long → drop
            {"bullet": "ok", "supporting_ids": ["a", "b", "c"]},        # valid
        ],
        "archives": [
            {"id": "", "reason": "blank"},                   # drop
            {"id": "mem_z"},                                 # valid (no reason)
        ],
    })
    out = fc._parse_proposals(raw)
    assert len(out["merges"]) == 1
    assert out["merges"][0]["keep"] == "y"
    assert len(out["soul"]) == 1
    assert out["soul"][0]["bullet"] == "ok"
    assert len(out["archives"]) == 1
    assert out["archives"][0]["id"] == "mem_z"


def test_parse_proposals_handles_garbage() -> None:
    assert fc._parse_proposals("") == {"merges": [], "soul": [], "archives": []}
    assert fc._parse_proposals("nothing") == {"merges": [], "soul": [], "archives": []}
    assert fc._parse_proposals("{not json}") == {"merges": [], "soul": [], "archives": []}
