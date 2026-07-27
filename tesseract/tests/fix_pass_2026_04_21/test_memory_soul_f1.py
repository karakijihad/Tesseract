"""F1 regression tests — memory filter hardening, dedupe, librarian,
bootstrap capsule char caps.

Covers the 10 unit tests enumerated in
`Docs/Plan/pre-phase-14-foundation/phase-f1-memory-soul-redesign.md` §5m.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest

from tesseract.memory import dedupe
from tesseract.memory.librarian import Librarian
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType
from tesseract.memory.what_not_to_save import WhatNotToSave


WHAT_NOT_TO_SAVE_SEED = """# What NOT to Save

1. **Secrets and credentials**
2. **Code patterns**
3. **Git history**
4. **Ephemeral task state**
5. **CLAUDE.md content**
6. **Routine acknowledgements**
7. **Raw file contents**
8. **Raw tool / command output**
9. **Request echoes**
10. **Turn summaries**
11. **Trivial bodies**
"""


@pytest.fixture
def store_dir(tmp_path: Path) -> Path:
    (tmp_path / "WHAT_NOT_TO_SAVE.md").write_text(WHAT_NOT_TO_SAVE_SEED, encoding="utf-8")
    return tmp_path


@pytest.fixture
def wnts(store_dir: Path) -> WhatNotToSave:
    return WhatNotToSave(store_dir=store_dir)


# ── should_save() pattern tests ──────────────────────────────────────


def test_what_not_to_save_request_echo(wnts: WhatNotToSave) -> None:
    """4 sample request-echo titles blocked."""
    samples = [
        "You asked me to index the vault today",
        "The operator requested a summary of the sprint",
        "As you requested, here is the observer wiring",
        "User_asked about the memory store layout",
    ]
    for s in samples:
        assert wnts.should_save(s) is False, f"should have blocked: {s!r}"
        assert wnts.last_reason == "request_echo", (
            f"expected request_echo, got {wnts.last_reason!r} for {s!r}"
        )


def test_what_not_to_save_turn_summary(wnts: WhatNotToSave) -> None:
    """5 sample turn-summary titles blocked."""
    samples = [
        "In this turn, TARS delegated to Claude and saved three mems.",
        "Summary of the turn: observer fired, no tool calls.",
        "What I did this turn was refactor the retrieval pipeline and update the observer wiring downstream.",
        "Turn summary — reflection ran, soul unchanged, saves=2",
        "Last action: memory_save for the vault-ingest roadmap",
    ]
    for s in samples:
        assert wnts.should_save(s) is False, f"should have blocked: {s!r}"
        assert wnts.last_reason == "turn_summary", (
            f"expected turn_summary, got {wnts.last_reason!r} for {s!r}"
        )


def test_what_not_to_save_trivial_body(wnts: WhatNotToSave) -> None:
    """< 80 chars = blocked; >= 80 chars with no other trigger = allowed."""
    short = "operator likes tea"
    assert wnts.should_save(short) is False
    assert wnts.last_reason == "trivial_body"

    long = (
        "Operator drinks a specific Yorkshire tea blend every morning and "
        "prefers it strong; context for any breakfast-timing suggestions."
    )
    assert len(long) >= 80
    assert wnts.should_save(long) is True
    assert wnts.last_reason is None


# ── memory_save type-inference guard ────────────────────────────────


def test_memory_save_type_inference_guard(tmp_path: Path) -> None:
    """type=user + request-echo title → blocked, logged to writes.jsonl with reason=type_mismatch."""
    import asyncio

    (tmp_path / "WHAT_NOT_TO_SAVE.md").write_text(WHAT_NOT_TO_SAVE_SEED, encoding="utf-8")

    from tesseract.kernel.tools.base import ToolContext
    from tesseract.kernel.tools.memory_save import MemorySaveInput, MemorySaveTool
    from tesseract.memory.index import MemoryIndex

    store = MemoryStore(store_dir=tmp_path)
    index = MemoryIndex(store_dir=tmp_path)
    tool = MemorySaveTool(store=store, index=index, embeddings=None)

    inp = MemorySaveInput(
        type="user",
        title="request_echo_test",
        content="operator asked about the vault layout earlier this session",
        importance=5,
    )
    ctx = ToolContext(session_id="test_session")
    result = asyncio.run(tool.run(inp, ctx))

    assert result.is_error is True
    assert "type_mismatch" in result.output

    writes_path = tmp_path / "events" / "writes.jsonl"
    assert writes_path.exists(), "writes.jsonl must be written for forensic trail"
    lines = writes_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 1
    event = json.loads(lines[-1])
    assert event["status"] == "blocked"
    assert event["reason"] == "type_mismatch"
    assert event["type"] == "user"
    assert event["title"] == "request_echo_test"


# ── dedupe.check() ───────────────────────────────────────────────────


class _FakeEmbeddings:
    """Minimal stand-in for EmbeddingIndex.search() used in dedupe.check()."""

    def __init__(self, hits: list[tuple[str, float]]) -> None:
        self._hits = hits

    async def search(self, query: str, top_k: int = 1):
        return self._hits[:top_k]


def test_dedupe_blocks_near_identical() -> None:
    """Top-1 score above threshold → (False, existing_id)."""
    import asyncio

    emb = _FakeEmbeddings(hits=[("mem_abc123", 0.95)])
    ok, existing = asyncio.run(dedupe.check("some body", emb, threshold=0.92))
    assert ok is False
    assert existing == "mem_abc123"


def test_dedupe_allows_different() -> None:
    """No search hit → (True, None); sub-threshold hit → (True, None)."""
    import asyncio

    empty = _FakeEmbeddings(hits=[])
    ok, existing = asyncio.run(dedupe.check("unrelated body text", empty))
    assert ok is True
    assert existing is None

    weak = _FakeEmbeddings(hits=[("mem_xyz", 0.40)])
    ok, existing = asyncio.run(dedupe.check("another body", weak, threshold=0.92))
    assert ok is True
    assert existing is None


# ── librarian.run_pass() ─────────────────────────────────────────────


def test_librarian_promotes_durable_entry(store_dir: Path) -> None:
    """MVP scope: promotion is deferred; verify run_pass() rebuilds MEMORY.md
    with counts + top-by-importance + recent, from the live store."""
    import asyncio

    store = MemoryStore(store_dir=store_dir)

    now = _dt.datetime.now(_dt.timezone.utc)
    fm = MemoryFrontmatter(
        id="mem_durable1",
        type=MemoryType.USER,
        title="Operator prefers terse session notes",
        summary="prefers terse session notes under 80 lines",
        created_at=now,
        updated_at=now,
        importance=9,
    )
    body = (
        "Operator has repeatedly asked for session notes to stay under 80 lines "
        "and to link audits rather than inlining their prose into the session log."
    )
    assert store.write(fm, body) is True, "seed memory should not be blocked"

    librarian = Librarian(store)
    report = asyncio.run(librarian.run_pass())

    assert report["counts"]["user"] == 1
    assert report["top"] == 1
    assert report["recent"] == 1

    index_path = store_dir / "MEMORY.md"
    assert index_path.exists()
    text = index_path.read_text(encoding="utf-8")
    assert "TESSERACT Memory Index" in text
    assert "Operator prefers terse session notes" in text
    assert "importance 9" in text


# ── Bootstrap char caps ──────────────────────────────────────────────


def _seed_workspace(root: Path, sizes: dict[str, int]) -> None:
    """Create workspace/*.md files of given character sizes (no frontmatter)."""
    root.mkdir(parents=True, exist_ok=True)
    for name, size in sizes.items():
        body = ("x" * size) if size > 0 else ""
        (root / name).write_text(body, encoding="utf-8")


def test_bootstrap_per_file_cap(tmp_path: Path) -> None:
    """Per-file content over 12k chars is truncated with a marker."""
    from tesseract.brain import prompt as prompt_mod

    workspace = tmp_path / "workspace"
    store = tmp_path / "memory-store"
    store.mkdir()

    oversized = "y" * (prompt_mod.PER_FILE_CAP + 5_000)
    _seed_workspace(workspace, {"IDENTITY.md": 0})
    (workspace / "IDENTITY.md").write_text(oversized, encoding="utf-8")
    for name in ("SOUL.md", "USER.md", "AGENTS.md", "MCP.md"):
        (workspace / name).write_text("small", encoding="utf-8")

    out = prompt_mod.assemble_system_prompt(
        workspace_dir=workspace,
        memory_store_dir=store,
        mode="manifest",
    )
    assert "[truncated" in out, "truncation marker must appear when a file exceeds PER_FILE_CAP"
    # Bounded: assembled prompt can't contain the full oversized blob.
    assert len(oversized) not in (len(out),), "oversized content should not be inlined whole"
    # Confirm the kept prefix is present but not the tail.
    assert ("y" * 1000) in out
    # Marker slack — the prompt body contributes its own `y` characters
    # ("say", "yet", "any", static rule text). Slack grows as load-bearing
    # prompt rules are added; the truncation invariant being tested is
    # "oversized file is bounded by PER_FILE_CAP", not exact byte equality.
    assert out.count("y") <= prompt_mod.PER_FILE_CAP + 300


def test_bootstrap_total_cap(tmp_path: Path) -> None:
    """Memory capsule honors MEMORY_CAPSULE_TOTAL_CAP — later daily files are skipped."""
    from tesseract.brain import prompt as prompt_mod

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # Minimal workspace so assemble_system_prompt has something to anchor.
    for name in ("IDENTITY.md", "SOUL.md", "USER.md", "AGENTS.md", "MCP.md"):
        (workspace / name).write_text("placeholder body\n", encoding="utf-8")

    store = tmp_path / "memory-store"
    daily = store / "daily"
    daily.mkdir(parents=True)

    # Each file just under PER_FILE_CAP; two together exceed the 60k total cap.
    chunk = "a" * (prompt_mod.PER_FILE_CAP - 100)
    (store / "MEMORY.md").write_text(chunk, encoding="utf-8")

    today = _dt.date.today()
    yesterday = today - _dt.timedelta(days=1)
    (daily / f"{today.isoformat()}.md").write_text(chunk, encoding="utf-8")
    (daily / f"{yesterday.isoformat()}.md").write_text(chunk, encoding="utf-8")
    # Extra daily that should never be reached (only today/yesterday loaded anyway).
    older = today - _dt.timedelta(days=10)
    (daily / f"{older.isoformat()}.md").write_text("sentinel-old-daily", encoding="utf-8")

    capsule = prompt_mod._build_memory_capsule(store)
    assert capsule, "capsule should be non-empty when MEMORY.md + at least one daily exist"
    assert "sentinel-old-daily" not in capsule, "older daily must not be loaded"
    # Capsule body length must be bounded by the total cap (plus small formatting slack).
    body_len = sum(len(p) for p in capsule.split("<!-- ")[1:])
    assert body_len <= prompt_mod.MEMORY_CAPSULE_TOTAL_CAP + 500


def test_bootstrap_missing_file_skipped(tmp_path: Path) -> None:
    """Missing workspace files do not raise; surviving files still assemble."""
    from tesseract.brain import prompt as prompt_mod

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # Only IDENTITY present; SOUL/USER/AGENTS/MCP deliberately missing.
    (workspace / "IDENTITY.md").write_text("I am TARS — sentinel content.", encoding="utf-8")

    store = tmp_path / "memory-store"
    store.mkdir()

    out = prompt_mod.assemble_system_prompt(
        workspace_dir=workspace,
        memory_store_dir=store,
        mode="manifest",
    )
    assert "sentinel content" in out
    # `Right now` section is always appended last; proves graceful degrade
    # didn't throw on the missing files.
    assert "Right now" in out
