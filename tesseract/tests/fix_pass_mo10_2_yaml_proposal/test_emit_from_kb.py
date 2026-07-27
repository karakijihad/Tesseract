"""MO-10-2 §2i — knowledge-keeper emit path diffs KB vs providers.yaml."""

from __future__ import annotations

import textwrap
from pathlib import Path

import yaml

from tesseract.knowledge_keeper.emit import emit_proposals_for_provider
from tesseract.workspace_events import EventStore


def _seed_providers_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "providers.yaml"
    p.write_text(
        textwrap.dedent(
            """\
            availability:
              max_consecutive_failures: 3
            chain:
              transient_retries: 2
              transient_backoff_ms: 250
              cooldown_max_failures: 1
              cooldown_seconds: 60
            cost_tracking:
              enabled: true
              warning_at_pct: 0.75
              log_file: logs/cost-tracking.jsonl
            api:
              anthropic:
                enabled: false
                api_key_env: ANTHROPIC_API_KEY
                adapter: anthropic
                models:
                  opus_47:
                    model: claude-opus-4-7
                    context_window: 1000000
                    cost_per_mtok_in: 15.0
                    cost_per_mtok_out: 75.0
            cli: {}
            local: {}
            """
        ),
        encoding="utf-8",
    )
    return p


def _seed_kb_file(tmp_path: Path, models: list[dict]) -> Path:
    kb_dir = tmp_path / "knowledge-base" / "providers"
    kb_dir.mkdir(parents=True)
    kb = kb_dir / "anthropic.md"
    fm = {"provider": "anthropic", "canonical_models": models}
    body = "# Anthropic — knowledge base\n"
    kb.write_text(
        "---\n" + yaml.safe_dump(fm, sort_keys=False).rstrip() + "\n---\n\n" + body,
        encoding="utf-8",
    )
    return kb


def test_emit_detects_new_model(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    providers_yaml = _seed_providers_yaml(tmp_path)
    kb_file = _seed_kb_file(
        tmp_path,
        [
            {"id": "opus_47", "context_window": 1000000},
            {"id": "claude_4_8", "model": "claude-4.8", "context_window": 1000000},
        ],
    )
    store = EventStore(tmp_path / "logs")
    emitted = emit_proposals_for_provider(
        kb_file=kb_file,
        provider_slug="anthropic",
        providers_yaml=providers_yaml,
        event_store=store,
    )
    assert len(emitted) == 1
    events = store.list_events()
    assert len(events) == 1
    assert events[0].kind == "yaml_change_proposal"
    assert events[0].payload["kind_origin"] == "provider_model_added"
    assert events[0].payload["yaml_path"] == "api.anthropic.models.claude_4_8"


def test_emit_dedup_skips_second_run(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    providers_yaml = _seed_providers_yaml(tmp_path)
    kb_file = _seed_kb_file(
        tmp_path,
        [{"id": "opus_47"}, {"id": "claude_4_8", "model": "claude-4.8"}],
    )
    store = EventStore(tmp_path / "logs")
    emit_proposals_for_provider(
        kb_file=kb_file,
        provider_slug="anthropic",
        providers_yaml=providers_yaml,
        event_store=store,
    )
    again = emit_proposals_for_provider(
        kb_file=kb_file,
        provider_slug="anthropic",
        providers_yaml=providers_yaml,
        event_store=store,
    )
    assert again == []


def test_emit_detects_pricing_change(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    providers_yaml = _seed_providers_yaml(tmp_path)
    kb_file = _seed_kb_file(
        tmp_path,
        [{"id": "opus_47", "pricing_input_per_mtok_usd": 18.0}],
    )
    store = EventStore(tmp_path / "logs")
    # Note: catalog stores cost_per_mtok_in; the KB shape uses pricing_*.
    # The diff fires because the KB field name differs from catalog —
    # which is the expected behavior in v1 (KB carries the canonical
    # surface; the operator decides whether to wire the proposal into
    # the catalog field name they've standardized on). This test pins
    # behavior so future refactors don't silently drop the signal.
    emitted = emit_proposals_for_provider(
        kb_file=kb_file,
        provider_slug="anthropic",
        providers_yaml=providers_yaml,
        event_store=store,
    )
    assert emitted, "expected pricing-changed proposal"
    kinds = {e.payload["kind_origin"] for e in store.list_events()}
    assert "provider_pricing_changed" in kinds
