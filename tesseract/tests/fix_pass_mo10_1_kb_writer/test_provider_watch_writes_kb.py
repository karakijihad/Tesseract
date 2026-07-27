"""MO-10-1 §2b — `_write_provider_kb` writes anthropic.md through merge."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from tesseract.kernel.tools.base import ToolContext
from tesseract.scheduler.tasks.provider_watch import _write_provider_kb


class _FakeToolResult:
    def __init__(self, output: str) -> None:
        self.output = output
        self.is_error = False


class _FakeTavily:
    async def run(self, inp, ctx):  # noqa: ANN001
        return _FakeToolResult("• Claude Opus 4.7 shipped at https://example.com/anthropic/news")


def test_write_provider_kb_creates_anthropic_md(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.provider_watch.TavilySearchTool",
        lambda: _FakeTavily(),
    )

    search_ctx = ToolContext(
        workspace_root=str(tmp_path),
        session_id="t-provider-watch",
        current_call_id="call-1",
    )
    summary = asyncio.run(
        _write_provider_kb(
            providers=[
                {"name": "Anthropic", "queries": ["claude release"]},
                {"name": "OpenAI", "queries": ["gpt release"]},
                {"name": "Ecosystem", "queries": ["langchain release"]},
            ],
            target_date=date(2026, 5, 15),
            search_ctx=search_ctx,
            max_results=3,
            tavily_call_cap=5,
        )
    )
    assert "anthropic.md" in summary["refreshed"]
    anth = tmp_path / "vault" / "knowledge-base" / "providers" / "anthropic.md"
    assert anth.is_file()
    text = anth.read_text(encoding="utf-8")
    assert "provider: anthropic" in text
    assert "canonical_models:" in text
    assert "Recent changes" in text
    # Ecosystem isn't in the provider slug map — it must be skipped, not crash.
    assert not (tmp_path / "vault" / "knowledge-base" / "providers" / "ecosystem.md").exists()
    # refresh log row appended
    log = tmp_path / "vault" / "knowledge-base" / "providers" / "_refresh-log.jsonl"
    assert log.read_text(encoding="utf-8").strip()
