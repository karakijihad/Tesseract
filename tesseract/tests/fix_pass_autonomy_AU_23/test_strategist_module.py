"""AU-23 — pure primitives in `orchestrator/autonomy/strategist.py`.

Covers:
- Initiative validation (boundary checks on confidence / horizon / criteria)
- Risk class coercion (AUTONOMOUS / ABSOLUTE_DENY → PROPOSE)
- parse_response lenient (drops bad entries, keeps good ones)
- filter_initiatives confidence threshold + max cap + deterministic order
- dedup ledger round-trip (write → read → filter)
- collect_inputs idle short-circuit on empty TESSERACT_HOME
- collect_inputs reads agenda index, leaf index, vault log, worker fails, paused sources
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tesseract.orchestrator.autonomy.strategist import (
    DEFAULT_HORIZON_DAYS,
    DEFAULT_MIN_CONFIDENCE,
    Initiative,
    append_seen,
    build_prompt,
    collect_inputs,
    dedupe_against_ledger,
    filter_initiatives,
    initiative_key,
    parse_response,
    read_seen,
    seen_ledger_path,
)
from tesseract.orchestrator.workers.record import RiskClass


# ── Initiative model ────────────────────────────────────────────────


def test_initiative_minimum_fields():
    i = Initiative(
        slug="example-init",
        goal="Pick up the new SDK and refresh the wiki page.",
        rationale="Two worker failures traced to outdated SDK calls.",
        success_criteria=["wiki page mentions 0.45"],
        confidence=0.7,
        horizon_days=5,
    )
    assert i.slug == "example-init"
    assert i.suggested_risk_class is RiskClass.PROPOSE


def test_initiative_slug_kebab_normalised():
    i = Initiative(
        slug="Mixed_Case Slug!!",
        goal="x" * 15,
        rationale="y" * 15,
        success_criteria=["ok"],
        confidence=0.7,
        horizon_days=3,
    )
    assert i.slug == "mixed-case-slug"


def test_initiative_rejects_empty_success_criteria():
    with pytest.raises(Exception):
        Initiative(
            slug="x",
            goal="x" * 15,
            rationale="y" * 15,
            success_criteria=[],
            confidence=0.7,
            horizon_days=3,
        )


def test_initiative_confidence_boundary():
    # Out of range raises.
    with pytest.raises(Exception):
        Initiative(
            slug="x",
            goal="x" * 15,
            rationale="y" * 15,
            success_criteria=["ok"],
            confidence=1.5,
            horizon_days=3,
        )


@pytest.mark.parametrize("risk_in", ["autonomous", "absolute_deny", "bogus", ""])
def test_initiative_risk_class_coerces_to_propose(risk_in):
    i = Initiative(
        slug="x",
        goal="x" * 15,
        rationale="y" * 15,
        success_criteria=["ok"],
        confidence=0.7,
        horizon_days=3,
        suggested_risk_class=risk_in,
    )
    assert i.suggested_risk_class is RiskClass.PROPOSE


def test_initiative_operator_gate_preserved():
    i = Initiative(
        slug="x",
        goal="x" * 15,
        rationale="y" * 15,
        success_criteria=["ok"],
        confidence=0.7,
        horizon_days=3,
        suggested_risk_class="operator_gate",
    )
    assert i.suggested_risk_class is RiskClass.OPERATOR_GATE


# ── parse_response ──────────────────────────────────────────────────


def test_parse_response_handles_empty():
    assert parse_response("") == []
    assert parse_response("   ") == []
    assert parse_response("no json here") == []


def test_parse_response_drops_bad_entries_keeps_good_ones():
    raw = json.dumps({
        "initiatives": [
            {  # good
                "slug": "good",
                "goal": "ingest the new SDK docs",
                "rationale": "two worker failures",
                "success_criteria": ["wiki updated"],
                "confidence": 0.8,
                "horizon_days": 5,
            },
            {  # bad — empty success_criteria
                "slug": "bad",
                "goal": "x" * 15,
                "rationale": "y" * 15,
                "success_criteria": [],
                "confidence": 0.9,
                "horizon_days": 3,
            },
            {  # good
                "slug": "also-good",
                "goal": "rotate the tavily key",
                "rationale": "key is overdue",
                "success_criteria": ["new key in .env"],
                "confidence": 0.65,
                "horizon_days": 2,
            },
        ]
    })
    out = parse_response(raw)
    assert len(out) == 2
    assert {i.slug for i in out} == {"good", "also-good"}


def test_parse_response_extracts_object_with_preamble():
    raw = "Some preamble.\n\n" + json.dumps({"initiatives": [{
        "slug": "x",
        "goal": "x" * 15,
        "rationale": "y" * 15,
        "success_criteria": ["ok"],
        "confidence": 0.7,
        "horizon_days": 3,
    }]})
    out = parse_response(raw)
    assert len(out) == 1


# ── filter_initiatives ──────────────────────────────────────────────


def _mk(slug: str, confidence: float) -> Initiative:
    return Initiative(
        slug=slug,
        goal=f"{slug}-goal goes here",
        rationale="r" * 15,
        success_criteria=["ok"],
        confidence=confidence,
        horizon_days=3,
    )


def test_filter_drops_below_threshold():
    out = filter_initiatives([_mk("a", 0.59), _mk("b", 0.6)])
    assert [i.slug for i in out] == ["b"]


def test_filter_caps_to_max_count_keeps_top_confidence():
    out = filter_initiatives(
        [_mk("a", 0.95), _mk("b", 0.75), _mk("c", 0.85), _mk("d", 0.65)],
        max_count=2,
    )
    assert [i.slug for i in out] == ["a", "c"]


def test_filter_is_deterministic_for_ties():
    out = filter_initiatives(
        [_mk("zebra", 0.8), _mk("apple", 0.8), _mk("mango", 0.8)],
        max_count=3,
    )
    # confidence equal → slug alphabetical tiebreak
    assert [i.slug for i in out] == ["apple", "mango", "zebra"]


# ── dedup ledger ────────────────────────────────────────────────────


def test_ledger_round_trip(tmp_path: Path):
    path = tmp_path / "strategist-seen.jsonl"
    when = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    i1 = _mk("init-a", 0.8)
    i2 = _mk("init-b", 0.7)
    append_seen(path, initiative=i1, when=when)
    append_seen(path, initiative=i2, when=when)
    seen = read_seen(path, now=when + timedelta(hours=1), window_days=14)
    assert initiative_key(i1) in seen
    assert initiative_key(i2) in seen


def test_ledger_drops_entries_outside_window(tmp_path: Path):
    path = tmp_path / "strategist-seen.jsonl"
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    new = datetime(2026, 5, 20, tzinfo=timezone.utc)
    append_seen(path, initiative=_mk("ancient", 0.8), when=old)
    append_seen(path, initiative=_mk("recent", 0.8), when=new)
    seen = read_seen(path, now=new + timedelta(hours=1), window_days=14)
    assert initiative_key(_mk("ancient", 0.8)) not in seen
    assert initiative_key(_mk("recent", 0.8)) in seen


def test_dedupe_against_ledger_drops_known():
    when = datetime(2026, 5, 20, tzinfo=timezone.utc)
    seen = {initiative_key(_mk("a", 0.8))}
    fresh = dedupe_against_ledger(
        [_mk("a", 0.8), _mk("b", 0.8)],
        seen=seen,
    )
    assert [i.slug for i in fresh] == ["b"]


# ── collect_inputs ──────────────────────────────────────────────────


def test_collect_inputs_idle_when_empty(tmp_path: Path):
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    inputs = collect_inputs(app=None, now=now, lookback_days=7, tesseract_home=tmp_path)
    assert inputs.is_idle()
    assert inputs.window_end_iso == now.isoformat()


def test_collect_inputs_reads_agenda_index(tmp_path: Path):
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    agenda = tmp_path / "agenda"
    agenda.mkdir()
    with (agenda / "index.jsonl").open("w", encoding="utf-8") as fh:
        for ts_offset_days, item_id, to_status in [
            (1, "ag-old", "done"),       # inside window
            (10, "ag-ancient", "done"),  # outside window (older than 7d)
        ]:
            row = {
                "item_id": item_id,
                "ts": (now - timedelta(days=ts_offset_days)).isoformat(),
                "event": "transition",
                "from_status": "running",
                "to_status": to_status,
                "reason": "ok",
                "goal": "do a thing",
                "source": "strategist",
            }
            fh.write(json.dumps(row) + "\n")
    inputs = collect_inputs(app=None, now=now, lookback_days=7, tesseract_home=tmp_path)
    ids = [r["id"] for r in inputs.agenda_recent]
    assert "ag-old" in ids
    assert "ag-ancient" not in ids


def test_collect_inputs_reads_leaf_index_keeps_admitted_buffered_sealed(tmp_path: Path):
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    leaf_idx = tmp_path / "memory-store" / "leaves" / "index.jsonl"
    leaf_idx.parent.mkdir(parents=True)
    with leaf_idx.open("w", encoding="utf-8") as fh:
        for leaf_id, state in [
            ("leaf-1", "pending"),    # dropped
            ("leaf-2", "admitted"),   # kept
            ("leaf-3", "buffered"),   # kept
            ("leaf-4", "sealed"),     # kept
            ("leaf-5", "dropped"),    # dropped
        ]:
            row = {
                "leaf_id": leaf_id,
                "ts": (now - timedelta(hours=2)).isoformat(),
                "state": state,
                "source": "docs",
                "title": f"title-{leaf_id}",
            }
            fh.write(json.dumps(row) + "\n")
    inputs = collect_inputs(app=None, now=now, lookback_days=7, tesseract_home=tmp_path)
    ids = [r["leaf_id"] for r in inputs.discovery_leaves]
    assert set(ids) == {"leaf-2", "leaf-3", "leaf-4"}


def test_collect_inputs_reads_worker_failures(tmp_path: Path):
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    workers = tmp_path / "workers" / "2026-05-19"
    workers.mkdir(parents=True)
    for worker_id, status in [
        ("w-ok", "done"),
        ("w-fail", "failed"),
        ("w-deny", "denied"),
    ]:
        (workers / f"{worker_id}.json").write_text(json.dumps({
            "worker_id": worker_id,
            "status": status,
            "kind": "tars_self",
            "goal": f"goal for {worker_id}",
            "updated_at": (now - timedelta(hours=1)).isoformat(),
            "created_at": (now - timedelta(hours=2)).isoformat(),
        }), encoding="utf-8")
    inputs = collect_inputs(app=None, now=now, lookback_days=7, tesseract_home=tmp_path)
    ids = sorted(r["worker_id"] for r in inputs.failed_workers)
    assert ids == ["w-deny", "w-fail"]


def test_collect_inputs_reads_paused_sources(tmp_path: Path):
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    autonomy = tmp_path / "autonomy"
    autonomy.mkdir()
    (autonomy / "governor-paused.json").write_text(json.dumps({
        "observer": {"reason": "too noisy", "paused_at": "2026-05-15T00:00:00+00:00"},
    }), encoding="utf-8")
    inputs = collect_inputs(app=None, now=now, lookback_days=7, tesseract_home=tmp_path)
    assert inputs.paused_sources
    assert inputs.paused_sources[0]["source"] == "observer"


# ── prompt ──────────────────────────────────────────────────────────


def test_build_prompt_embeds_window_header():
    now = datetime(2026, 5, 20, tzinfo=timezone.utc)
    inputs = collect_inputs(app=None, now=now, lookback_days=3)
    text = build_prompt(inputs)
    assert "Window:" in text
    assert "Return the JSON object now." in text


# ── path helpers ────────────────────────────────────────────────────


def test_seen_ledger_path_under_tesseract_home(tmp_path: Path):
    # path helper is call-time, mirroring AgendaStore/heartbeat pattern
    path = seen_ledger_path(tmp_path)
    assert path == tmp_path / "autonomy" / "strategist-seen.jsonl"
