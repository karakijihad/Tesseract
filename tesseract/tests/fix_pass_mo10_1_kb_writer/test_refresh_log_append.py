"""MO-10-1 §2f — `_refresh-log.jsonl` is append-only and schema-shaped."""

from __future__ import annotations

import json

from tesseract.knowledge_keeper import append_refresh_row, ensure_kb_tree


def test_append_writes_jsonl_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    base = ensure_kb_tree()
    log = append_refresh_row(
        base / "providers",
        file="anthropic.md",
        diff_summary="+1 model",
    )
    log2 = append_refresh_row(
        base / "providers",
        file="openai.md",
        diff_summary="no changes",
    )
    assert log == log2
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 2
    assert {"ts", "file", "diff_summary"}.issubset(rows[0].keys())
    assert rows[0]["file"] == "anthropic.md"
    assert rows[1]["file"] == "openai.md"
