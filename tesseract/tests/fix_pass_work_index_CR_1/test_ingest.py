"""CR-1: end-to-end ingestion — session JSON + workshop markdown.

Idempotent ingest: re-ingesting the same file yields the same row
count (per-path delete-then-add inside ``index_*_file``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tesseract.memory.work_index import WorkIndex
from tesseract.memory.work_ingester import (
    backfill,
    index_session_file,
    index_workshop_file,
)


def _write_session(path: Path, history: list[dict]) -> None:
    path.write_text(json.dumps({
        "schema": "session.v1",
        "started_at": "2026-05-22T10:00:00+00:00",
        "history": history,
    }), encoding="utf-8")


def test_ingest_session_emits_per_message_chunks(tmp_path: Path) -> None:
    sess = tmp_path / "2026-05-22.json"
    _write_session(sess, [
        {"role": "user", "content": "workshop memory question"},
        {"role": "assistant", "content": "let me check"},
        {"role": "tool", "content": "tool result skipped by default"},
        {"role": "user", "content": "thanks"},
    ])
    idx = WorkIndex(tmp_path / "work.sqlite")
    n = index_session_file(idx, sess)
    assert n == 3, f"expected 3 (no tool); got {n}"
    hits = idx.search("workshop", top_k=5)
    assert len(hits) == 1
    assert hits[0].source == "session"
    assert hits[0].source_ref == "2026-05-22"


def test_ingest_workshop_markdown(tmp_path: Path) -> None:
    ws = tmp_path / "tars-workshop" / "2026-05-22" / "entity-autonomy-plan" / "README.md"
    ws.parent.mkdir(parents=True, exist_ok=True)
    ws.write_text(
        "# Entity Autonomy Plan\n\nIntro paragraph.\n\n"
        "## Phase 1\n\nVertical slice with delegation.\n",
        encoding="utf-8",
    )
    idx = WorkIndex(tmp_path / "work.sqlite")
    n = index_workshop_file(idx, ws)
    assert n >= 2
    hits = idx.search("vertical slice", top_k=5)
    assert len(hits) >= 1
    assert hits[0].source == "workshop"
    assert hits[0].source_ref == "entity-autonomy-plan"


def test_ingest_is_idempotent(tmp_path: Path) -> None:
    sess = tmp_path / "2026-05-22.json"
    _write_session(sess, [
        {"role": "user", "content": "msg one"},
        {"role": "assistant", "content": "reply"},
    ])
    idx = WorkIndex(tmp_path / "work.sqlite")
    n1 = index_session_file(idx, sess)
    n2 = index_session_file(idx, sess)
    n3 = index_session_file(idx, sess)
    assert n1 == n2 == n3 == 2
    assert idx.count() == 2, "re-ingest must not duplicate rows"


def test_backfill_walks_both_corpora(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_session(sessions / "s1.json",
                   [{"role": "user", "content": "alpha"}])
    _write_session(sessions / "s2.json",
                   [{"role": "user", "content": "beta"}])
    workshop = tmp_path / "tars-workshop"
    (workshop / "2026-05-22" / "art").mkdir(parents=True)
    (workshop / "2026-05-22" / "art" / "README.md").write_text(
        "# Art\n\nbody", encoding="utf-8"
    )
    idx = WorkIndex(tmp_path / "work.sqlite")
    counts = backfill(idx, sessions_dir=sessions, workshop_dir=workshop)
    assert counts["sessions"] == 2
    assert counts["workshop"] >= 1


def test_backfill_missing_dirs_noop(tmp_path: Path) -> None:
    idx = WorkIndex(tmp_path / "work.sqlite")
    counts = backfill(
        idx,
        sessions_dir=tmp_path / "nope1",
        workshop_dir=tmp_path / "nope2",
    )
    assert counts == {"sessions": 0, "workshop": 0}


def test_ingest_session_skips_bad_json(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    idx = WorkIndex(tmp_path / "work.sqlite")
    n = index_session_file(idx, bad)
    assert n == 0
    assert idx.count() == 0
