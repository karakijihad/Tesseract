"""Codex audit 2026-05-19 P1 #3 — ``resolve_role_runtime`` is the
helper that lets ``invoke_agent`` honor ``agent.model_role``.

Returns ``None`` in three cases the caller must handle:

* role name is empty / ``chat_brain`` (caller already has it),
* role missing or inactive in roles.yaml,
* no chain entry has a buildable adapter (e.g. every fallback's API
  key is absent in this env).

The first case is asserted here; the others are environment-dependent
and asserted in integration / live test passes.
"""

from __future__ import annotations

from tesseract.brain.boot import resolve_role_runtime


def test_resolve_role_runtime_returns_none_for_empty_or_chat_brain() -> None:
    assert resolve_role_runtime("") is None
    assert resolve_role_runtime(None) is None  # type: ignore[arg-type]
    assert resolve_role_runtime("chat_brain") is None


def test_resolve_role_runtime_returns_none_for_unknown_role() -> None:
    assert resolve_role_runtime("nonexistent_role_xyz") is None


def test_resolve_role_runtime_returns_none_when_no_adapters_buildable() -> None:
    """In a test env with no API keys set, every fallback in
    ``agents_default`` should fail to build → caller falls back to
    parent adapter gracefully (no exception raised)."""
    # Don't assert None directly — operator may have keys live in their
    # .env when running tests. Just assert the helper never raises and
    # returns a typed shape OR None.
    result = resolve_role_runtime("agents_default")
    if result is not None:
        primary_cfg, primary_adapter, primary_options, chain = result
        assert primary_options.role == "agents_default"
        assert len(chain) >= 1
