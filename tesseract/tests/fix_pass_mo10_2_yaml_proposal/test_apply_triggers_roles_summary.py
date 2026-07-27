"""MO-10-2 §2g — approving a yaml_change_proposal triggers SUMMARY regen.

Tests the SUMMARY regen call directly to keep the gate small; the
``_commit_yaml_change_proposal`` helper is exercised end-to-end in the
Playwright test (out-of-scope for unit tests).
"""

from __future__ import annotations

from tesseract.scripts.regenerate_roles_summary import regenerate


def test_regenerate_after_catalog_edit(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    out = regenerate()
    assert out.is_file()
    text = out.read_text(encoding="utf-8")
    assert "TARS roles — current wiring" in text
