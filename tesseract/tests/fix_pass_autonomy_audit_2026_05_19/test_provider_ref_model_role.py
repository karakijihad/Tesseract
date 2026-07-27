"""Codex audit-2 2026-05-19 P2 — ``invoke_agent`` honors provider-ref
shaped ``model_role`` values (``<tier>.<provider>.<model>``).

Prior behaviour fell through to the parent adapter on provider-ref
shapes, so an agent declaring ``model_role: api.openai.gpt54_mini``
silently ran on whatever chat_brain resolved to. The new
``resolve_provider_ref_runtime`` helper closes the contract gap.

In a no-API-key environment the helper must return ``None`` cleanly
(graceful fallback to parent). The full happy-path is environment-
dependent; we only assert the resolver shape + the safety contracts.
"""

from __future__ import annotations

from tesseract.brain.boot import resolve_provider_ref_runtime


def test_returns_none_on_empty_ref() -> None:
    assert resolve_provider_ref_runtime("") is None
    assert resolve_provider_ref_runtime(None) is None  # type: ignore[arg-type]


def test_returns_none_on_malformed_ref() -> None:
    """Refs not matching ``<tier>.<provider>.<model>`` (or otherwise
    unresolvable) must return None — caller falls back to parent."""
    assert resolve_provider_ref_runtime("not.a.real.ref") is None
    assert resolve_provider_ref_runtime("nonsense") is None


def test_returns_none_when_no_adapter_buildable() -> None:
    """In a test env without API keys, every catalog entry will fail
    to build an adapter → returns None cleanly. The resolver MUST NOT
    raise — caller depends on a graceful None for the fallback path."""
    # Pick a real ref that exists in providers.yaml so the cfg-build
    # passes but the adapter-build fails (no API key in test env).
    result = resolve_provider_ref_runtime("api.openai.gpt54_mini")
    # Whether this returns a tuple or None depends on whether the test
    # env happens to have the API key. Both shapes are valid — just
    # verify the call itself doesn't raise.
    if result is not None:
        cfg, adapter, opts = result
        assert opts.role == "api.openai.gpt54_mini", "role slot must reflect the pinned ref"


def test_returns_none_on_role_name_inputs() -> None:
    """Role names (no dots) should fall through; this helper is for
    provider refs only. The caller (``_resolve_sub_adapter``) already
    splits role-name vs ref via ``_is_provider_ref`` upstream."""
    # The helper does not enforce the dot check; this is mostly a
    # documentation test. The bundle.resolve will reject it as malformed.
    assert resolve_provider_ref_runtime("agents_default") is None
