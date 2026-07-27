"""AU-24 — ecosystem-radar pre-fetcher unit tests.

Pure I/O. The pre-fetcher walks four directories under TESSERACT_HOME
and folds the last-N-days slice of each into one payload the
``ecosystem-digest`` agent consumes. The renderer integration test
lives in ``test_ecosystem_renderer_integration.py``.

Per CLAUDE.md log-safety: every test monkeypatches ``TESSERACT_HOME``
before any writer is exercised, even though this module exercises only
the pre-fetcher (which is pure reads).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tesseract.orchestrator.brief.ecosystem import (
    DEFAULT_SINCE_DAYS,
    MAX_PER_STREAM,
    collect_ecosystem_inputs,
    has_any_signal,
)


@pytest.fixture(autouse=True)
def _tesseract_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def today() -> date:
    return date(2026, 5, 20)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat()


# ────────────────────────────────────────────────────────────────────
# 1. All four streams empty → payload has every list empty and
#    ``has_any_signal`` reports False.
# ────────────────────────────────────────────────────────────────────


def test_empty_home_returns_empty_streams(tmp_path: Path, today: date) -> None:
    payload = collect_ecosystem_inputs(home=tmp_path, target_date=today)
    assert payload["since_days"] == DEFAULT_SINCE_DAYS
    assert payload["target_date"] == today.isoformat()
    assert payload["memory_signals"] == []
    assert payload["memory_leaves"] == []
    assert payload["docs_watch"] == []
    assert payload["provider_watch"] == []
    assert has_any_signal(payload) is False


# ────────────────────────────────────────────────────────────────────
# 2. memory_signal agenda items inside the window are picked up; older
#    items + items from a different source are excluded.
# ────────────────────────────────────────────────────────────────────


def test_memory_signals_window_and_source_filter(
    tmp_path: Path, today: date
) -> None:
    agenda_active = tmp_path / "agenda" / "active"
    agenda_active.mkdir(parents=True)
    in_window = _iso(datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=2))
    too_old = _iso(datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=30))
    (agenda_active / "ag_a.json").write_text(json.dumps({
        "id": "ag_a",
        "source": "memory_signal",
        "goal": "review memory signal (topic_surfaced): claude pricing changed",
        "rationale": "memory_signal: topic_surfaced | summary: claude pricing changed | entities: claude, pricing",
        "created_at": in_window,
    }))
    (agenda_active / "ag_old.json").write_text(json.dumps({
        "id": "ag_old",
        "source": "memory_signal",
        "goal": "old signal",
        "rationale": "memory_signal: stale",
        "created_at": too_old,
    }))
    (agenda_active / "ag_other.json").write_text(json.dumps({
        "id": "ag_other",
        "source": "observer",
        "goal": "non-memory-signal",
        "rationale": "observer ping",
        "created_at": in_window,
    }))
    payload = collect_ecosystem_inputs(home=tmp_path, target_date=today)
    assert len(payload["memory_signals"]) == 1
    row = payload["memory_signals"][0]
    assert row["kind"] == "topic_surfaced"
    assert "claude pricing changed" in row["goal"]
    assert "claude" in row["rationale"]
    assert has_any_signal(payload) is True


# ────────────────────────────────────────────────────────────────────
# 3. MemoryLeaf records inside the window are picked up; archive +
#    active are both walked because AU-11c leaves seal into archive
#    quickly via ExtractChunkJob.
# ────────────────────────────────────────────────────────────────────


def test_memory_leaves_active_and_archive_both_picked_up(
    tmp_path: Path, today: date
) -> None:
    active = tmp_path / "memory-store" / "leaves" / "active"
    archive = tmp_path / "memory-store" / "leaves" / "archive" / "2026-05"
    active.mkdir(parents=True)
    archive.mkdir(parents=True)
    now_iso = _iso(datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc) - timedelta(hours=2))
    (active / "leaf_abc12345.json").write_text(json.dumps({
        "id": "leaf_abc12345",
        "source": "discovery_feed:anthropic-news",
        "title": "Claude haiku 5 ships",
        "body": "Anthropic released claude-haiku-4-5.",
        "entities": ["claude-haiku-4-5", "anthropic"],
        "state": "pending_extraction",
        "created_at": now_iso,
    }))
    (archive / "leaf_def67890.json").write_text(json.dumps({
        "id": "leaf_def67890",
        "source": "discovery_feed:openai-changelog",
        "title": "GPT pricing change",
        "body": "OpenAI cut input token cost.",
        "entities": ["gpt-5", "openai", "pricing"],
        "state": "sealed",
        "created_at": now_iso,
    }))
    payload = collect_ecosystem_inputs(home=tmp_path, target_date=today)
    titles = {row["title"] for row in payload["memory_leaves"]}
    assert titles == {"Claude haiku 5 ships", "GPT pricing change"}


# ────────────────────────────────────────────────────────────────────
# 4. Docs-watch snapshots — the .md mtime gates inclusion; stale
#    snapshots are skipped.
# ────────────────────────────────────────────────────────────────────


def test_docs_watch_snapshots_filter_by_mtime(
    tmp_path: Path, today: date
) -> None:
    snap_dir = tmp_path / "autonomy" / "watchlist-snapshots"
    snap_dir.mkdir(parents=True)
    fresh = snap_dir / "claude_code_docs.md"
    fresh.write_text(
        "# Claude Code\n\nNew slash command landed.", encoding="utf-8"
    )
    stale = snap_dir / "openclaw.md"
    stale.write_text("# OpenClaw\n\nUnchanged content.", encoding="utf-8")
    # Stale = 30 days ago.
    stale_ts = (
        datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
        - timedelta(days=30)
    ).timestamp()
    import os
    os.utime(stale, (stale_ts, stale_ts))
    payload = collect_ecosystem_inputs(home=tmp_path, target_date=today)
    sources = {row["source"] for row in payload["docs_watch"]}
    assert sources == {"claude_code_docs"}
    row = payload["docs_watch"][0]
    assert "New slash command" in row["preview"]
    # Codex audit 2026-05-20 §M4 — source URLs come through the
    # bundled autonomy-watchlist.yaml so the digester can cite real
    # upstream docs pages.
    assert row["source_urls"], "claude_code_docs has urls: in autonomy-watchlist.yaml"
    assert row["url"] == row["source_urls"][0]
    assert row["url"].startswith("https://docs.anthropic.com/")


# ────────────────────────────────────────────────────────────────────
# 5. provider_watch daily digests — the iso-date filename gates
#    inclusion (filename is more reliable than mtime since cron
#    overwrites can update mtime arbitrarily).
# ────────────────────────────────────────────────────────────────────


def test_provider_watch_window_by_filename(
    tmp_path: Path, today: date
) -> None:
    digests = tmp_path / "memory-store" / "daily" / "providers"
    digests.mkdir(parents=True)
    (digests / f"{today.isoformat()}.md").write_text(
        "# Provider watch -- 2026-05-20\n\nAnthropic shipped a new sonnet.",
        encoding="utf-8",
    )
    in_window = today - timedelta(days=3)
    (digests / f"{in_window.isoformat()}.md").write_text(
        "# Provider watch\n\nOpenAI raised pricing.",
        encoding="utf-8",
    )
    too_old = today - timedelta(days=20)
    (digests / f"{too_old.isoformat()}.md").write_text(
        "# Provider watch\n\nAncient entry.",
        encoding="utf-8",
    )
    payload = collect_ecosystem_inputs(home=tmp_path, target_date=today)
    dates = {row["date"] for row in payload["provider_watch"]}
    assert dates == {today.isoformat(), in_window.isoformat()}


# ────────────────────────────────────────────────────────────────────
# 6. Per-stream caps — each stream is bounded so the prompt token
#    budget stays inside the ``agents_default`` context window.
# ────────────────────────────────────────────────────────────────────


def test_per_stream_cap_applied(tmp_path: Path, today: date) -> None:
    active = tmp_path / "memory-store" / "leaves" / "active"
    active.mkdir(parents=True)
    base = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    for i in range(MAX_PER_STREAM + 5):
        (active / f"leaf_{i:08x}.json").write_text(json.dumps({
            "id": f"leaf_{i:08x}",
            "source": "discovery_feed",
            "title": f"Item {i}",
            "body": "body",
            "entities": [],
            "state": "pending_extraction",
            "created_at": _iso(base - timedelta(minutes=i)),
        }))
    payload = collect_ecosystem_inputs(home=tmp_path, target_date=today)
    assert len(payload["memory_leaves"]) == MAX_PER_STREAM
    # Sort is descending by created_at → the freshest item wins.
    assert payload["memory_leaves"][0]["title"] == "Item 0"


# ────────────────────────────────────────────────────────────────────
# 7. Malformed JSON in agenda or leaves directory is logged + skipped
#    without raising. A single corrupt file must not abort the whole
#    pre-fetch.
# ────────────────────────────────────────────────────────────────────


# ────────────────────────────────────────────────────────────────────
# 8. Codex audit 2026-05-20 §M4 — source URL provenance across all
#    four streams. Strategist + digester need citable URLs; missing
#    provenance is the deliberate exception (empty string), never a
#    fabricated value.
# ────────────────────────────────────────────────────────────────────


def test_source_url_provenance_across_streams(tmp_path: Path, today: date) -> None:
    base = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)

    # memory_signal — URL embedded in rationale.
    agenda_active = tmp_path / "agenda" / "active"
    agenda_active.mkdir(parents=True)
    (agenda_active / "ag_sig.json").write_text(json.dumps({
        "id": "ag_sig",
        "source": "memory_signal",
        "goal": "review memory signal (topic_surfaced): anthropic pricing changed",
        "rationale": (
            "memory_signal: topic_surfaced | "
            "summary: see https://www.anthropic.com/pricing for the new rate"
        ),
        "created_at": _iso(base - timedelta(hours=4)),
    }))

    # memory_leaf — URL embedded in body.
    leaves = tmp_path / "memory-store" / "leaves" / "active"
    leaves.mkdir(parents=True)
    (leaves / "leaf_aabbccdd.json").write_text(json.dumps({
        "id": "leaf_aabbccdd",
        "source": "discovery_feed:anthropic-news",
        "title": "Claude haiku 5 ships",
        "body": "Anthropic released claude-haiku-4-5. Details at https://www.anthropic.com/news/haiku-4-5.",
        "entities": ["claude-haiku-4-5"],
        "state": "pending_extraction",
        "created_at": _iso(base - timedelta(hours=2)),
    }))

    # docs_watch — URL from the bundled autonomy-watchlist.yaml.
    snap_dir = tmp_path / "autonomy" / "watchlist-snapshots"
    snap_dir.mkdir(parents=True)
    (snap_dir / "claude_code_docs.md").write_text(
        "# Claude Code\n\nNew slash command landed.", encoding="utf-8"
    )

    # provider_watch — URL embedded in the daily digest preview.
    digests = tmp_path / "memory-store" / "daily" / "providers"
    digests.mkdir(parents=True)
    (digests / f"{today.isoformat()}.md").write_text(
        "# Provider watch -- "
        f"{today.isoformat()}\n\nAnthropic shipped a new sonnet — "
        "https://www.anthropic.com/news/sonnet-update.",
        encoding="utf-8",
    )

    payload = collect_ecosystem_inputs(home=tmp_path, target_date=today)

    sig = payload["memory_signals"][0]
    assert sig["url"] == "https://www.anthropic.com/pricing"

    leaf = payload["memory_leaves"][0]
    assert leaf["url"] == "https://www.anthropic.com/news/haiku-4-5"

    docs = payload["docs_watch"][0]
    assert docs["source"] == "claude_code_docs"
    assert docs["source_urls"], "watchlist must seed docs-watch source URLs"
    assert docs["url"].startswith("https://docs.anthropic.com/")

    prov = payload["provider_watch"][0]
    assert prov["url"] == "https://www.anthropic.com/news/sonnet-update"


def test_source_url_absent_falls_back_to_empty_string(
    tmp_path: Path, today: date
) -> None:
    """No URL anywhere → ``url`` is the empty string, never fabricated."""
    leaves = tmp_path / "memory-store" / "leaves" / "active"
    leaves.mkdir(parents=True)
    (leaves / "leaf_noourl0.json").write_text(json.dumps({
        "id": "leaf_noourl0",
        "source": "operator_note",
        "title": "Manual entry",
        "body": "No upstream URL referenced.",
        "entities": [],
        "state": "pending_extraction",
        "created_at": _iso(datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc) - timedelta(hours=1)),
    }))
    payload = collect_ecosystem_inputs(home=tmp_path, target_date=today)
    assert payload["memory_leaves"][0]["url"] == ""


def test_malformed_records_are_skipped(tmp_path: Path, today: date) -> None:
    active = tmp_path / "memory-store" / "leaves" / "active"
    active.mkdir(parents=True)
    (active / "leaf_garbled.json").write_text("not-valid-json{{{")
    in_window = _iso(datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc) - timedelta(hours=1))
    (active / "leaf_aaaaaaaa.json").write_text(json.dumps({
        "id": "leaf_aaaaaaaa",
        "source": "discovery_feed",
        "title": "Valid item",
        "body": "ok",
        "entities": [],
        "state": "pending_extraction",
        "created_at": in_window,
    }))
    payload = collect_ecosystem_inputs(home=tmp_path, target_date=today)
    titles = [row["title"] for row in payload["memory_leaves"]]
    assert titles == ["Valid item"]
