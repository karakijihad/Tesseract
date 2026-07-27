"""WS-3 — preferred_coder threads schema → wire message → session record."""

from __future__ import annotations

import tesseract.config.loader as loader_mod
from tesseract.orchestrator.tars_controller.dispatcher import _resolve_default_coder
from tesseract.orchestrator.tars_controller.protocol import NewSessionMessage
from tesseract.orchestrator.tars_controller.sessions import SessionRegistry


class _FakeBundle:
    def __init__(self, raw: dict) -> None:
        self.roles_raw = raw


def test_new_session_message_accepts_preferred_coder():
    msg = NewSessionMessage(
        msg="new_session", mode="chat", origin="mirror", preferred_coder="claude"
    )
    assert msg.preferred_coder == "claude"


def test_new_session_message_defaults_none():
    msg = NewSessionMessage(msg="new_session", mode="chat", origin="mirror")
    assert msg.preferred_coder is None


def test_create_session_persists_preferred_coder(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    rec = SessionRegistry().create_session(
        mode="chat", origin="mirror", preferred_coder="claude",
    )
    reloaded = SessionRegistry().get_session(rec.session_id)
    assert reloaded.preferred_coder == "claude"


def test_resolve_default_coder_reads_valid_yaml_value(monkeypatch):
    monkeypatch.setattr(
        loader_mod, "load_config", lambda *a, **k: _FakeBundle({"coder_default": "codex"})
    )
    assert _resolve_default_coder() == "codex"


def test_resolve_default_coder_missing_key_is_none(monkeypatch):
    monkeypatch.setattr(loader_mod, "load_config", lambda *a, **k: _FakeBundle({}))
    assert _resolve_default_coder() is None


def test_resolve_default_coder_invalid_value_is_none(monkeypatch):
    monkeypatch.setattr(
        loader_mod, "load_config", lambda *a, **k: _FakeBundle({"coder_default": "gpt"})
    )
    assert _resolve_default_coder() is None


def test_resolve_default_coder_swallows_config_error(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("bad config")

    monkeypatch.setattr(loader_mod, "load_config", _boom)
    assert _resolve_default_coder() is None


def test_resolve_default_coder_matches_real_yaml():
    """The coder_default key added to roles.yaml is actually wired."""
    from tesseract.config.loader import load_config

    expected = load_config().roles_raw.get("coder_default")
    result = _resolve_default_coder()
    if expected in ("claude", "codex"):
        assert result == expected
    else:
        assert result is None
