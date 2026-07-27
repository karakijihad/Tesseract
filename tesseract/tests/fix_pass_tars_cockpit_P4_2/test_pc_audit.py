from __future__ import annotations

import json

import pytest

from tesseract.orchestrator.browser.pc_audit import append_pc_audit_row, pc_audit_path


@pytest.mark.asyncio
async def test_append_writes_one_row(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    await append_pc_audit_row(
        tool="browser_navigate", input={"url": "https://example.com"},
        posture="ask", result_summary="navigated", session_id="s-test",
    )
    path = pc_audit_path()
    assert path.exists()
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert row["tool"] == "browser_navigate"
    assert row["posture"] == "ask"
    assert row["decision"] == "approved"
    assert row["input"] == {"url": "https://example.com"}
    assert row["session_id"] == "s-test"
    assert "at_utc" in row
