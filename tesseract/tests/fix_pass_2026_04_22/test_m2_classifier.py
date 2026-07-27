"""Regression suite for memory-retune M2 — librarian prefix routing + classifier.

Covers:
  * `[user|feedback|project|reference]` prefixes route sections into the
    matching `memory-store/<type>/` subdir (no classifier call needed).
  * Missing-prefix sections call `classify_section` on the wired adapter.
  * Classifier confidence < 0.6 → section skipped + `writes.jsonl` entry.
  * Classifier timeout (adapter raises `asyncio.TimeoutError`) → skipped.
  * Adapter `None` + unknown prefix → skipped (offline-safe path).
  * `[chat_digest]` maps to REFERENCE + carries `chat_digest` tag.

Contract: `Docs/Plan/memory-retune/_shared/librarian-classifier-contract.md`.
"""
from __future__ import annotations

import asyncio
import json
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
from tesseract.memory.types import MemoryType


# ── Helpers ───────────────────────────────────────────────────────────────


class _FakeAdapter(ModelAdapter):
    """ModelAdapter stub.

    `generate()` is the only surface the classifier exercises; the other
    abstract methods are stubbed to satisfy the ABC.
    """

    def __init__(
        self,
        response: str = "",
        *,
        raise_exc: BaseException | None = None,
    ) -> None:
        self._response = response
        self._raise_exc = raise_exc
        self.call_count = 0

    async def generate(self, prompt: str, options: AdapterOptions | None = None) -> str:
        self.call_count += 1
        if self._raise_exc is not None:
            raise self._raise_exc
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


def _seed_daily(tmp_path: Path, date_stem: str, sections: list[tuple[str, str]]) -> Path:
    """Write a daily/<date>.md file with given `## <title>` + body pairs."""
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    path = daily_dir / f"{date_stem}.md"
    parts: list[str] = [f"# {date_stem}", ""]
    for title, body in sections:
        parts.append(f"## {title}")
        parts.append(body)
        parts.append("")
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def _read_writes_events(tmp_path: Path) -> list[dict]:
    events = tmp_path / "events" / "writes.jsonl"
    if not events.exists():
        return []
    return [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines() if line.strip()]


# Body bank — all >80 chars, none matching WhatNotToSave patterns.
_USER_BODY = (
    "The operator runs two workstations: a dev box with an 8GB VRAM card and a "
    "Dell with 6GB VRAM. Models swap between them depending on the task."
)
_FEEDBACK_BODY = (
    "Session notes should stay terse — link audits and plan files rather than "
    "pasting their contents. Match the surrounding entries, never exceed them."
)
_PROJECT_BODY = (
    "The Mirror rewire remains in flight; the plan folder at Docs/Plan/mirror "
    "will land once the scheduler phase clears its audit queue."
)
_REFERENCE_BODY = (
    "FAISS periodic rebuild hook is implemented by rebuild_from_store and can "
    "be triggered from the dreaming heartbeat once wiring is added."
)
_UNPREFIXED_BODY = (
    "A durable note about retrieval ranking — the cosine floor sits at 0.72 "
    "today but can move with calibration once production data accumulates."
)
_DIGEST_BODY = (
    "Chat digest for 2026-04-21: operator steered the memory retune phase "
    "plan and confirmed the terse session-note rule during the evening pass."
)


# ── Tests ─────────────────────────────────────────────────────────────────


async def test_prefix_routes_to_correct_type(tmp_path: Path) -> None:
    store = MemoryStore(store_dir=tmp_path)
    _seed_daily(tmp_path, "2024-01-01", [
        ("[user] Operator dev hardware", _USER_BODY),
        ("[feedback] Terse session notes", _FEEDBACK_BODY),
        ("[project] Mirror rewire status", _PROJECT_BODY),
        ("[reference] FAISS rebuild hook", _REFERENCE_BODY),
    ])

    librarian = Librarian(store=store, embeddings=None, adapter=None)
    result = await librarian.run_pass()

    assert result["promoted"] == 4
    assert len(store.list_all(type_filter=MemoryType.USER)) == 1
    assert len(store.list_all(type_filter=MemoryType.FEEDBACK)) == 1
    assert len(store.list_all(type_filter=MemoryType.PROJECT)) == 1
    assert len(store.list_all(type_filter=MemoryType.REFERENCE)) == 1


async def test_missing_prefix_calls_classifier(tmp_path: Path) -> None:
    store = MemoryStore(store_dir=tmp_path)
    _seed_daily(tmp_path, "2024-01-02", [
        ("Retrieval ranking note", _UNPREFIXED_BODY),
    ])

    adapter = _FakeAdapter(response='{"type": "project", "confidence": 0.9}')
    librarian = Librarian(store=store, embeddings=None, adapter=adapter)
    result = await librarian.run_pass()

    assert adapter.call_count == 1, "classifier must be invoked for unprefixed sections"
    assert result["promoted"] == 1
    projects = store.list_all(type_filter=MemoryType.PROJECT)
    assert len(projects) == 1
    assert projects[0].title == "Retrieval ranking note"


async def test_low_confidence_skips_section(tmp_path: Path) -> None:
    store = MemoryStore(store_dir=tmp_path)
    _seed_daily(tmp_path, "2024-01-03", [
        ("Ambiguous note", _UNPREFIXED_BODY),
    ])

    adapter = _FakeAdapter(response='{"type": "user", "confidence": 0.4}')
    librarian = Librarian(store=store, embeddings=None, adapter=adapter)
    result = await librarian.run_pass()

    assert result["promoted"] == 0
    assert store.list_all() == []
    events = _read_writes_events(tmp_path)
    unclassifiable = [e for e in events if e.get("reason") == "unclassifiable"]
    assert len(unclassifiable) == 1
    assert unclassifiable[0]["status"] == "skipped"
    assert unclassifiable[0]["title"] == "Ambiguous note"


async def test_classifier_timeout_skips_section(tmp_path: Path) -> None:
    store = MemoryStore(store_dir=tmp_path)
    _seed_daily(tmp_path, "2024-01-04", [
        ("Slow-classifier note", _UNPREFIXED_BODY),
    ])

    adapter = _FakeAdapter(raise_exc=asyncio.TimeoutError())
    librarian = Librarian(store=store, embeddings=None, adapter=adapter)
    result = await librarian.run_pass()

    assert result["promoted"] == 0
    assert store.list_all() == []
    events = _read_writes_events(tmp_path)
    assert any(e.get("reason") == "unclassifiable" for e in events)


async def test_adapter_none_skips_unknown(tmp_path: Path) -> None:
    store = MemoryStore(store_dir=tmp_path)
    _seed_daily(tmp_path, "2024-01-05", [
        ("[user] Operator dev hardware", _USER_BODY),
        ("Unprefixed note without adapter", _UNPREFIXED_BODY),
    ])

    librarian = Librarian(store=store, embeddings=None, adapter=None)
    result = await librarian.run_pass()

    # Prefixed section still lands; unprefixed is skipped + logged.
    assert len(store.list_all(type_filter=MemoryType.USER)) == 1
    assert len(store.list_all(type_filter=MemoryType.REFERENCE)) == 0
    assert result["promoted"] == 1

    events = _read_writes_events(tmp_path)
    unclassifiable = [e for e in events if e.get("reason") == "unclassifiable"]
    assert len(unclassifiable) == 1
    assert unclassifiable[0]["title"] == "Unprefixed note without adapter"


async def test_chat_digest_maps_to_reference_with_tag(tmp_path: Path) -> None:
    store = MemoryStore(store_dir=tmp_path)
    _seed_daily(tmp_path, "2024-01-06", [
        ("[chat_digest] 2026-04-21", _DIGEST_BODY),
    ])

    librarian = Librarian(store=store, embeddings=None, adapter=None)
    await librarian.run_pass()

    refs = store.list_all(type_filter=MemoryType.REFERENCE)
    assert len(refs) == 1
    assert "chat_digest" in refs[0].tags
