"""ProviderWatchJob unit tests.

The job orchestrates: per-provider Tavily search → brief assembly →
adapter call → digest writeback. These tests monkeypatch Tavily and
the adapter chain so the test runs offline + deterministic.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tesseract.kernel.tools.base import ToolResult
from tesseract.scheduler.tasks.provider_watch import ProviderWatchJob
from tesseract.scheduler.types import JobContext


_FAKE_TAVILY_OUTPUT = (
    "Result 1: Claude 4.7 sonnet released today\n"
    "URL: https://www.anthropic.com/news/claude-4-7\n"
    "Snippet: New flagship model with 1M context window."
)


@pytest.mark.asyncio
async def test_provider_watch_writes_digest_on_happy_path(
    tmp_path: Path, monkeypatch
) -> None:
    """Tavily returns content for one provider, adapter renders digest,
    file lands at the configured path."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    fake_tavily = AsyncMock(return_value=ToolResult(output=_FAKE_TAVILY_OUTPUT))
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.provider_watch.TavilySearchTool.run",
        fake_tavily,
    )

    fake_digest = (
        "# Provider Watch — 2026-05-13\n\n"
        "## Anthropic\n"
        "- New: Claude 4.7 sonnet — 1M context window. "
        "https://www.anthropic.com/news/claude-4-7\n"
    )

    class _StubAdapter:
        async def generate(self, prompt, options):
            return fake_digest

    chain_built: list = [( _StubAdapter(), None)]
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.provider_watch.build_chain_for_role",
        lambda role_name, log_label="": chain_built,
    )

    digest_dir = tmp_path / "out"
    ctx = JobContext(
        job_name="provider_watch",
        fired_at=datetime(2026, 5, 13, 6, 30, tzinfo=timezone.utc),
        config={
            "providers": [
                {"name": "Anthropic", "queries": ["Anthropic Claude release"]},
            ],
            "digest_dir": str(digest_dir),
        },
    )

    result = await ProviderWatchJob().run(ctx)

    assert result.ok, result.detail
    assert result.payload["wrote"] is True
    out_path = digest_dir / "2026-05-13.md"
    assert out_path.exists()
    assert "Claude 4.7" in out_path.read_text(encoding="utf-8")
    # MO-10-1 extension: the job also writes KB files via a second
    # Tavily pass (the structured surface the yaml_change_proposal
    # diff reads). Once per query for digest + once per query for KB.
    assert fake_tavily.await_count >= 1


@pytest.mark.asyncio
async def test_provider_watch_skips_when_search_empty(
    tmp_path: Path, monkeypatch
) -> None:
    """Every Tavily call returns is_error — job exits ok=True with
    detail explaining the no-op, no file written."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    monkeypatch.setattr(
        "tesseract.scheduler.tasks.provider_watch.TavilySearchTool.run",
        AsyncMock(return_value=ToolResult(output="error", is_error=True)),
    )

    digest_dir = tmp_path / "out"
    ctx = JobContext(
        job_name="provider_watch",
        fired_at=datetime(2026, 5, 13, 6, 30, tzinfo=timezone.utc),
        config={
            "providers": [{"name": "Anthropic", "queries": ["x"]}],
            "digest_dir": str(digest_dir),
        },
    )

    result = await ProviderWatchJob().run(ctx)

    assert result.ok
    assert "no usable results" in result.detail
    assert not (digest_dir / "2026-05-13.md").exists()


@pytest.mark.asyncio
async def test_provider_watch_fails_loud_on_empty_providers_config(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    ctx = JobContext(
        job_name="provider_watch",
        fired_at=datetime(2026, 5, 13, 6, 30, tzinfo=timezone.utc),
        config={"providers": []},
    )
    result = await ProviderWatchJob().run(ctx)
    assert result.ok is False
    assert "empty" in result.detail
