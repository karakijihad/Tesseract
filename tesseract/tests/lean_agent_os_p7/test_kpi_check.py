"""P7 Task 1 — KPI-website check scheduler job.

Covers:
- happy path: fixture page fetched → extraction prompt sees the page text
  → delivered via fake channel adapter → memory write with source tag
- missing `urls` config → ok=False, detail names the key
- one URL fails, other succeeds → result carries both the KPI answer and
  the fetch-failed line; ok=True
- all URLs fail → ok=False
- no logs pollution under tesseract/logs/ from the test run
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tesseract import integrations as integrations_module
from tesseract.scheduler.tasks import kpi_check
from tesseract.scheduler.tasks.kpi_check import KpiCheckJob
from tesseract.scheduler.types import JobContext

# This file is tesseract/tests/lean_agent_os_p7/test_kpi_check.py, so
# parents[2] is `tesseract/`, not the repo root.
_REPO_LOGS = Path(__file__).resolve().parents[2] / "logs"


def _logs_snapshot() -> dict[str, int]:
    """Recursive, per-file-size snapshot — a top-level-name-only diff would
    miss an appended line to an existing file, which is exactly the
    zero-tolerance case the project's logs-pollution rule exists to catch."""
    if not _REPO_LOGS.exists():
        return {}
    return {
        str(p.relative_to(_REPO_LOGS)): p.stat().st_size
        for p in _REPO_LOGS.rglob("*")
        if p.is_file()
    }


@pytest.fixture(autouse=True)
def _guard_logs_pollution():
    """Zero-tolerance guard (CLAUDE.md): no test may write under
    tesseract/logs/. Snapshot at THIS test's setup and re-check at its
    teardown, so the comparison window spans only this test's own
    execution — not the whole collection→run span, which false-positives
    on any concurrent live-service write (mirror-backend.log, tokenjuice/,
    …) during a slow suite."""
    before = _logs_snapshot()
    yield
    after = _logs_snapshot()
    assert after == before, (
        f"tesseract/logs/ changed during this test. before={before} after={after}"
    )


# ── Fakes ───────────────────────────────────────────────────────────


class _FakeAdapter:
    def __init__(self, output: str = "", raise_exc: Exception | None = None):
        self.output = output
        self.raise_exc = raise_exc
        self.calls: list[tuple[str, Any]] = []

    async def generate(self, prompt: str, options) -> str:
        self.calls.append((prompt, options))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.output


class _FakeOptions:
    def __init__(self, provider: str = "fake", model: str = "model"):
        self.provider = provider
        self.model = model


class _FakeStore:
    def __init__(self):
        self.writes: list[tuple[Any, str, str | None]] = []

    def write(self, frontmatter, body, *, subdir_override=None, skip_wnts_check=False):
        self.writes.append((frontmatter, body, subdir_override))
        return True


class _FakeBundle:
    def __init__(self, store):
        self.store = store


class _FakeChannelAdapter:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, *, chat_ref: str, text: str) -> None:
        self.sent.append((chat_ref, text))


def _make_app(*, store: _FakeStore | None = None) -> dict[str, Any]:
    app: dict[str, Any] = {}
    if store is not None:
        app["memory_bundle"] = _FakeBundle(store)
    return app


def _ctx(*, app: dict[str, Any], config: dict[str, Any]) -> JobContext:
    return JobContext(
        job_name="kpi_check",
        fired_at=datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc),
        app=app,
        config=config,
    )


def _patch_fetcher(monkeypatch: pytest.MonkeyPatch, pages: dict[str, str | Exception]) -> None:
    async def _fetch(url: str) -> str:
        page = pages[url]
        if isinstance(page, Exception):
            raise page
        return page

    monkeypatch.setattr(kpi_check, "_make_page_fetcher", lambda: _fetch)


def _patch_chain(monkeypatch: pytest.MonkeyPatch, adapter: _FakeAdapter) -> None:
    monkeypatch.setattr(
        kpi_check, "build_chain_for_job", lambda *a, **k: [(adapter, _FakeOptions())],
    )


# ── Tests ───────────────────────────────────────────────────────────


async def test_happy_path_delivers_and_writes_memory(monkeypatch: pytest.MonkeyPatch):
    fixture_text = "Fixture KPI page: current uptime is 99.98% over the last 30 days."
    _patch_fetcher(monkeypatch, {"https://status.example.com": fixture_text})
    adapter = _FakeAdapter(output="https://status.example.com: uptime is 99.98%")
    _patch_chain(monkeypatch, adapter)

    channel_adapter = _FakeChannelAdapter()
    monkeypatch.setattr(integrations_module, "get_channel", lambda name: channel_adapter)

    store = _FakeStore()
    app = _make_app(store=store)
    ctx = _ctx(app=app, config={
        "urls": [{"url": "https://status.example.com", "extract": "current uptime percentage"}],
        "channel": "telegram",
        "chat_ref": "12345",
    })

    result = await KpiCheckJob().run(ctx)

    assert result.ok is True
    assert len(adapter.calls) == 1
    prompt, _ = adapter.calls[0]
    assert fixture_text in prompt
    assert "current uptime percentage" in prompt

    assert len(channel_adapter.sent) == 1
    chat_ref, text = channel_adapter.sent[0]
    assert chat_ref == "12345"
    assert "99.98%" in text

    assert len(store.writes) == 1
    fm, body, subdir = store.writes[0]
    assert fm.source_type == "kpi_check"
    assert "kpi_check" in fm.tags
    assert subdir == "reference/kpi_check"
    assert "99.98%" in body


async def test_missing_urls_config_fails(monkeypatch: pytest.MonkeyPatch):
    ctx = _ctx(app=_make_app(), config={})
    result = await KpiCheckJob().run(ctx)
    assert result.ok is False
    assert "urls" in result.detail


async def test_one_url_fails_other_succeeds(monkeypatch: pytest.MonkeyPatch):
    _patch_fetcher(monkeypatch, {
        "https://good.example.com": "Good page: revenue KPI is $42,000 this month.",
        "https://dead.example.com": RuntimeError("connection refused"),
    })
    adapter = _FakeAdapter(output="https://good.example.com: revenue is $42,000")
    _patch_chain(monkeypatch, adapter)

    store = _FakeStore()
    ctx = _ctx(app=_make_app(store=store), config={
        "urls": [
            {"url": "https://good.example.com", "extract": "monthly revenue"},
            {"url": "https://dead.example.com", "extract": "monthly revenue"},
        ],
    })

    result = await KpiCheckJob().run(ctx)

    assert result.ok is True
    body = store.writes[0][1]
    assert "fetch failed: https://dead.example.com" in body
    assert "$42,000" in body


async def test_all_urls_fail(monkeypatch: pytest.MonkeyPatch):
    _patch_fetcher(monkeypatch, {
        "https://dead1.example.com": RuntimeError("timeout"),
        "https://dead2.example.com": RuntimeError("timeout"),
    })
    ctx = _ctx(app=_make_app(), config={
        "urls": [
            {"url": "https://dead1.example.com", "extract": "x"},
            {"url": "https://dead2.example.com", "extract": "y"},
        ],
    })
    result = await KpiCheckJob().run(ctx)
    assert result.ok is False


class _EmptyStrTimeout(Exception):
    """Stand-in for httpx's timeout exception classes, whose ``str()`` is
    empty when raised with no message (e.g. ``httpx.ReadTimeout()``)."""

    def __str__(self) -> str:
        return ""


async def test_fetch_failure_with_empty_str_exc_logs_type_name(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
):
    """Regression for the live P7 gate finding: a fetch timeout logged as
    `kpi_check: fetch failed for https://pypi.org/project/httpx/ ()` —
    `str(exc)` is empty for httpx's timeout classes, so `%s` formatting on
    the bare exception produced a blank parenthetical with no diagnostic
    value. Must use `repr(exc)` (carries the type name) instead."""
    _patch_fetcher(monkeypatch, {
        "https://good.example.com": "Good page: revenue KPI is $42,000 this month.",
        "https://dead.example.com": _EmptyStrTimeout(),
    })
    adapter = _FakeAdapter(output="https://good.example.com: revenue is $42,000")
    _patch_chain(monkeypatch, adapter)

    store = _FakeStore()
    ctx = _ctx(app=_make_app(store=store), config={
        "urls": [
            {"url": "https://good.example.com", "extract": "monthly revenue"},
            {"url": "https://dead.example.com", "extract": "monthly revenue"},
        ],
    })

    with caplog.at_level("WARNING"):
        result = await KpiCheckJob().run(ctx)

    assert result.ok is True
    warning_lines = [
        r.getMessage() for r in caplog.records if "fetch failed" in r.getMessage()
    ]
    assert warning_lines, "expected a 'fetch failed' warning log line"
    assert "_EmptyStrTimeout" in warning_lines[0]
    assert warning_lines[0].strip().endswith("()") is False

    body = store.writes[0][1]
    assert "fetch failed: https://dead.example.com" in body
    assert "_EmptyStrTimeout" in body




def test_terse_live_shaped_result_persists_against_real_wnts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Regression for the live P7 gate finding: a legitimately terse KPI
    result (one URL, one-sentence answer) must not be blocked by
    WhatNotToSave's trivial-body floor. The bare LLM answer alone
    ("uptime is 99.98%.") is 17 chars — well under the 80-char floor —
    so this drives the REAL `WhatNotToSave.should_save()` (not a fake
    store, not `_write_memory`'s `skip_wnts_check=True` bypass) directly
    against the body `_compose_body` produces, proving the enrichment
    itself — not just the escape hatch — is what clears the floor."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    from tesseract.memory.store import MemoryStore
    from tesseract.memory.what_not_to_save import WhatNotToSave

    successes = [({"url": "https://status.example.com", "extract": "current uptime"}, "page text")]
    body = kpi_check._compose_body(successes, [], "uptime is 99.98%.")

    wnts = WhatNotToSave(store_dir=tmp_path / "memory-store")
    assert wnts.should_save(body) is True, wnts.last_reason

    store = MemoryStore(store_dir=tmp_path / "memory-store")
    mem_id = kpi_check._write_memory(
        store, body, when=datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc),
    )

    assert mem_id is not None
    assert store.find_file(mem_id) is not None
