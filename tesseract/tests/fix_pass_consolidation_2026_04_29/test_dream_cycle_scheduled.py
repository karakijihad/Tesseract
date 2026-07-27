"""Audit M2 regression — `dream_cycle` must be a real scheduler job that
consumes the recall log and promotes qualifying memories.

Before 2026-04-29 `DreamingEngine.run_cycle` existed but was not bound
to any handler in `tesseract/scheduler/tasks/`, and `schedule.yaml` had
no `dream_cycle` entry. The recall log was never harvested, so durable
memory consolidation never fired.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from tesseract.memory.dreaming import DreamingEngine
from tesseract.memory.index import MemoryIndex
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType
from tesseract.scheduler.tasks.dream_cycle import DreamCycleJob
from tesseract.scheduler.types import JobContext


def _schedule_yaml_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "schedule.yaml"


def test_dream_cycle_handler_in_schedule_yaml() -> None:
    raw = yaml.safe_load(_schedule_yaml_path().read_text(encoding="utf-8"))
    jobs = {j["name"]: j for j in raw["jobs"]}
    assert "dream_cycle" in jobs
    assert jobs["dream_cycle"]["handler"] == "tesseract.scheduler.tasks.dream_cycle.DreamCycleJob"
    assert jobs["dream_cycle"]["enabled"] is True


def test_dream_cycle_handler_module_loadable() -> None:
    """Scheduler engine validates handler whitelist on load. The handler
    module must exist and the class importable."""
    from tesseract.scheduler.engine import ALLOWED_HANDLER_PREFIXES

    handler_str = "tesseract.scheduler.tasks.dream_cycle.DreamCycleJob"
    assert any(handler_str.startswith(p) for p in ALLOWED_HANDLER_PREFIXES), (
        "dream_cycle handler must match the scheduler's whitelist prefix"
    )


def test_dream_cycle_promotes_qualifying_memory(tmp_path: Path) -> None:
    """Run-cycle integration: seed a memory + recall log entries that
    pass the threshold, run the job, verify promotion."""
    store_dir = tmp_path / "memory-store"
    derived_dir = store_dir / "derived"
    derived_dir.mkdir(parents=True, exist_ok=True)
    recall_log = derived_dir / "recall.jsonl"

    store = MemoryStore(store_dir=store_dir)
    index = MemoryIndex(store_dir=store_dir)

    now = datetime.now(timezone.utc)
    fm = MemoryFrontmatter(
        id="mem_dream-promotable",
        type=MemoryType.PROJECT,
        title="A heavily recalled memory",
        summary="A memory that would meet the dream threshold.",
        tags=[],
        entities=[],
        importance=8,
        created_at=now,
        updated_at=now,
    )
    body = (
        "A passage the model keeps returning to: it explains an architectural "
        "decision around session persistence and the way transcripts are folded "
        "into the canonical store. Detailed enough to clear the trivial-body gate."
    )
    assert store.write(fm, body) is True, "store.write must not be blocked by WhatNotToSave"

    # Seed enough recall events to clear thresholds: min recall_count=3,
    # min unique queries=2, min score=0.75. Score formula:
    #   frequency = min(count/10, 1.0) * 0.35
    #   relevance = min(avg_confidence, 1.0) * 0.35
    #   diversity = min(unique/5, 1.0) * 0.15
    #   recency  = 0.5^(days/14) * 0.15
    # 10 recalls + 5 unique queries + ~0.9 confidence + just-now → ~0.94.
    queries = [
        "question about session persistence",
        "how does the store survive restart",
        "what keeps transcripts after compact",
        "does data persist when I close the app",
        "what happens to memory between sessions",
    ]
    with recall_log.open("w", encoding="utf-8") as f:
        for i in range(10):
            entry = {
                "memory_id": "mem_dream-promotable",
                "query": queries[i % len(queries)],
                "confidence": 0.9,
                "timestamp": now.isoformat(),
            }
            f.write(json.dumps(entry) + "\n")

    engine = DreamingEngine(store=store, index=index, recall_log_path=recall_log)

    class _StubApp:
        def __init__(self, bundle):
            self._d = {"memory_bundle": bundle}

        def get(self, k):
            return self._d.get(k)

    class _StubBundle:
        def __init__(self, dreaming):
            self.dreaming = dreaming

    job = DreamCycleJob()
    ctx = JobContext(
        job_name="dream_cycle",
        run_id="test-run",
        app=_StubApp(_StubBundle(engine)),
        config={},
    )
    result = asyncio.run(job.run(ctx))
    assert result.ok, f"job failed: {result.detail}"
    assert "mem_dream-promotable" in (result.payload or {}).get("promoted", [])

    # Recall log should have been trimmed of promoted entries.
    remaining = recall_log.read_text(encoding="utf-8").strip()
    assert "mem_dream-promotable" not in remaining
