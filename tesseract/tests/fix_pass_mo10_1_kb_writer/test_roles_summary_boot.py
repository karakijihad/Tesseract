"""MO-10-1 §2d — Mirror boot regenerates SUMMARY.md."""

from __future__ import annotations


def test_boot_hook_invokes_regenerate(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    from tesseract.mirror.server.app import _regenerate_kb_roles_summary

    _regenerate_kb_roles_summary()
    summary = tmp_path / "vault" / "knowledge-base" / "roles" / "SUMMARY.md"
    assert summary.is_file()
    text = summary.read_text(encoding="utf-8")
    assert "TARS roles — current wiring" in text


def test_boot_hook_is_fail_soft(monkeypatch, tmp_path):
    """A broken catalog must not crash Mirror startup."""

    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    import tesseract.scripts.regenerate_roles_summary as mod

    def explode(*args, **kwargs):
        raise RuntimeError("simulated catalog parse failure")

    monkeypatch.setattr(mod, "regenerate", explode)
    from tesseract.mirror.server.app import _regenerate_kb_roles_summary

    # Must not raise.
    _regenerate_kb_roles_summary()
