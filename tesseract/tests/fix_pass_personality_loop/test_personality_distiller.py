"""Smoke tests for Librarian.distill_personality_candidates.

Covers the read path (diary + SOUL Growth), adapter call shape, candidate
parsing/dedup against existing Growth, and the always-write semantics on
`<store_dir>/pending_growth.md`.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import AsyncGenerator

from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ChunkType,
    ModelAdapter,
    StreamChunk,
)
from tesseract.memory.librarian import Librarian
from tesseract.memory.store import MemoryStore


SOUL_FIXTURE = """\
---
name: TARS
version: 1
---

# SOUL

## Vibe

Calm.

## Growth

- Operator wants opinions, not menus.

## Continuity

What survives.
"""


class _StubAdapter(ModelAdapter):
    """Captures the last prompt and returns a canned JSON string."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.last_prompt: str | None = None
        self.calls = 0

    async def stream(self, messages, tools=None, options=None) -> AsyncGenerator[StreamChunk, None]:
        yield StreamChunk(type=ChunkType.TEXT, text="")  # not used

    def count_tokens(self, messages) -> int:
        return 0

    async def check_available(self) -> bool:
        return True

    async def generate(self, prompt: str, options: AdapterOptions | None = None) -> str:
        self.calls += 1
        self.last_prompt = prompt
        return self.response


def _setup(tmp_path: Path, *, diary_days: list[tuple[str, str]] | None = None) -> tuple[Path, Path]:
    """Create a memory-store + workspace tree with optional diary entries.
    Returns `(store_dir, soul_path)`.
    """
    store_dir = tmp_path / "memory-store"
    store_dir.mkdir()
    diary_dir = store_dir / "diary"
    diary_dir.mkdir()
    if diary_days:
        for stem, body in diary_days:
            (diary_dir / f"{stem}.md").write_text(body, encoding="utf-8")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    soul = workspace / "SOUL.md"
    soul.write_text(SOUL_FIXTURE, encoding="utf-8")
    return store_dir, soul


def _librarian(store_dir: Path, adapter: ModelAdapter | None) -> Librarian:
    store = MemoryStore(store_dir=store_dir)
    return Librarian(store=store, embeddings=None, adapter=adapter)


async def test_no_adapter_returns_zero(tmp_path: Path):
    store_dir, soul = _setup(tmp_path, diary_days=[(date.today().isoformat(), "stuff")])
    lib = _librarian(store_dir, adapter=None)
    res = await lib.distill_personality_candidates(soul)
    assert res == {"candidates": 0, "reason": "adapter_offline"}
    # No file written when adapter is offline — heartbeat re-runs tomorrow.
    assert not (store_dir / "pending_growth.md").exists()


async def test_no_diary_writes_empty_file(tmp_path: Path):
    store_dir, soul = _setup(tmp_path, diary_days=None)
    adapter = _StubAdapter('{"candidates": []}')
    lib = _librarian(store_dir, adapter=adapter)
    res = await lib.distill_personality_candidates(soul)
    assert res == {"candidates": 0, "reason": "no_diary"}
    # Adapter should NOT be called when diary is empty.
    assert adapter.calls == 0
    body = (store_dir / "pending_growth.md").read_text(encoding="utf-8")
    assert "no candidates this pass" in body
    assert "no_diary" in body


async def test_candidates_written_and_growth_dedup_applied(tmp_path: Path):
    today = date.today().isoformat()
    diary = [
        (today, "**14:00**  Defaulted to a checklist again. He wanted my read, not options."),
    ]
    store_dir, soul = _setup(tmp_path, diary_days=diary)
    response = json.dumps({
        "candidates": [
            "Operator wants opinions, not menus.",          # duplicates existing Growth
            "Pushes back faster when I sound like a forklift.",
            "Notices when I drop the dry humor and sound generic.",
        ]
    })
    adapter = _StubAdapter(response)
    lib = _librarian(store_dir, adapter=adapter)
    res = await lib.distill_personality_candidates(soul, max_candidates=3)
    assert res["candidates"] == 2  # the duplicate was dropped

    body = (store_dir / "pending_growth.md").read_text(encoding="utf-8")
    assert "- Pushes back faster" in body
    assert "- Notices when I drop the dry humor" in body
    assert "Operator wants opinions" not in body  # dropped against existing Growth
    # Prompt got real diary + Growth context.
    assert adapter.last_prompt is not None
    assert "Defaulted to a checklist" in adapter.last_prompt
    assert "Operator wants opinions, not menus." in adapter.last_prompt


async def test_old_diary_files_outside_window_ignored(tmp_path: Path):
    old = (date.today() - timedelta(days=30)).isoformat()
    diary = [(old, "**09:00**  ancient entry that should not influence distillation")]
    store_dir, soul = _setup(tmp_path, diary_days=diary)
    adapter = _StubAdapter('{"candidates": []}')
    lib = _librarian(store_dir, adapter=adapter)
    res = await lib.distill_personality_candidates(soul, days=7)
    # No diary inside the 7-day window → adapter never called.
    assert res == {"candidates": 0, "reason": "no_diary"}
    assert adapter.calls == 0


async def test_malformed_adapter_response_yields_zero(tmp_path: Path):
    today = date.today().isoformat()
    store_dir, soul = _setup(
        tmp_path, diary_days=[(today, "**12:00**  short observation")]
    )
    adapter = _StubAdapter("this is not json at all")
    lib = _librarian(store_dir, adapter=adapter)
    res = await lib.distill_personality_candidates(soul)
    assert res == {"candidates": 0}
    body = (store_dir / "pending_growth.md").read_text(encoding="utf-8")
    assert "no candidates this pass" in body


async def test_oversize_candidate_truncated(tmp_path: Path):
    today = date.today().isoformat()
    store_dir, soul = _setup(tmp_path, diary_days=[(today, "**12:00**  obs")])
    big = "x" * 1000
    adapter = _StubAdapter(json.dumps({"candidates": [big]}))
    lib = _librarian(store_dir, adapter=adapter)
    res = await lib.distill_personality_candidates(soul, max_candidates=3)
    assert res["candidates"] == 1
    body = (store_dir / "pending_growth.md").read_text(encoding="utf-8")
    # Should be capped — far shorter than 1000 chars on that line.
    candidate_line = next(line for line in body.splitlines() if line.startswith("- "))
    assert 0 < len(candidate_line) < 260


async def test_missing_soul_returns_reason(tmp_path: Path):
    today = date.today().isoformat()
    store_dir, soul = _setup(tmp_path, diary_days=[(today, "**12:00**  obs")])
    soul.unlink()  # remove SOUL.md after setup
    adapter = _StubAdapter('{"candidates": []}')
    lib = _librarian(store_dir, adapter=adapter)
    res = await lib.distill_personality_candidates(soul)
    assert res == {"candidates": 0, "reason": "soul_missing"}
    # Adapter not called when SOUL is missing.
    assert adapter.calls == 0
