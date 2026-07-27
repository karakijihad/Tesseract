"""Codex audit 2026-05-19 P0 #1 — runner must not pass a *model role*
name (e.g. ``"agents_default"``) as an *agent slug* to ``invoke_agent``.

Prior bug: 15 of 16 live autonomy workers failed with
``Unknown agent: 'agents_default'`` because the kernel stored
``self._rationale_role`` into ``WorkerRecord.role`` and the runner
treated that field as a slug. Fix is two-sided:

* Kernel passes ``role=""`` to ``build_worker_record`` so the field is
  the explicit agent-slug pin (empty by default).
* Runner's ``_route_for_kind`` falls back to ``tars-self`` / kind
  defaults when the field is empty OR matches a known model-role name
  OR is a provider-ref shape — defends against legacy records on disk
  and operator-pinned items that mis-use the field.
"""

from __future__ import annotations

from tesseract.orchestrator.autonomy.kernel_worker_runner import (
    DEFAULT_MARKDOWN_AGENT,
    DEFAULT_TARS_SELF_AGENT,
    _resolve_agent_slug,
    _route_for_kind,
)
from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.record import (
    WorkerRecord,
    RiskClass,
    WorkerStatus,
)
from datetime import datetime, timezone


def _make_record(*, kind: WorkerKind, role: str, prompt: str = "do the thing") -> WorkerRecord:
    moment = datetime.now(timezone.utc)
    return WorkerRecord(
        id=f"wk-test-{kind.value}-{role or 'empty'}",
        kind=kind,
        created_at=moment,
        updated_at=moment,
        agenda_item_id="ag-test",
        risk_class=RiskClass.AUTONOMOUS,
        role=role,
        prompt=prompt,
        status=WorkerStatus.QUEUED,
    )


def test_resolve_agent_slug_empty_uses_default() -> None:
    assert _resolve_agent_slug("", "tars-self") == "tars-self"
    assert _resolve_agent_slug(None, "tars-self") == "tars-self"


def test_resolve_agent_slug_known_model_roles_use_default() -> None:
    for role in ("agents_default", "subagents_default", "chat_brain", "observer_agent", "autonomy_heartbeat"):
        assert _resolve_agent_slug(role, "tars-self") == "tars-self", (
            f"model-role {role!r} should fall back to default, not be treated as slug"
        )


def test_resolve_agent_slug_provider_ref_uses_default() -> None:
    # Provider-ref shape (<tier>.<provider>.<model>) is not an agent slug.
    assert _resolve_agent_slug("api.openai.gpt54_mini", "tars-self") == "tars-self"
    assert _resolve_agent_slug("api.nim.gpt_oss_120b", "tars-self") == "tars-self"


def test_resolve_agent_slug_real_slug_passes_through() -> None:
    # Hyphenated names are real agent slugs; let them through.
    assert _resolve_agent_slug("research-brief", "tars-self") == "research-brief"
    assert _resolve_agent_slug("tars-self", "tars-self") == "tars-self"
    assert _resolve_agent_slug("provider-watcher", "tars-self") == "provider-watcher"


def test_route_tars_self_empty_role_uses_default_slug() -> None:
    record = _make_record(kind=WorkerKind.TARS_SELF, role="")
    tool_name, args = _route_for_kind(record)
    assert tool_name == "invoke_agent"
    assert args["name"] == DEFAULT_TARS_SELF_AGENT
    assert args["task"] == "do the thing"


def test_route_tars_self_model_role_no_longer_fails_dispatch() -> None:
    """Regression: ``role="agents_default"`` used to result in
    ``invoke_agent(name="agents_default")`` which failed with
    ``Unknown agent``. Now it falls back to ``tars-self``."""
    record = _make_record(kind=WorkerKind.TARS_SELF, role="agents_default")
    tool_name, args = _route_for_kind(record)
    assert tool_name == "invoke_agent"
    assert args["name"] == DEFAULT_TARS_SELF_AGENT


def test_route_markdown_agent_empty_role_uses_default() -> None:
    record = _make_record(kind=WorkerKind.MARKDOWN_AGENT, role="")
    tool_name, args = _route_for_kind(record)
    assert tool_name == "invoke_agent"
    assert args["name"] == DEFAULT_MARKDOWN_AGENT


def test_route_markdown_agent_real_slug_pins() -> None:
    record = _make_record(kind=WorkerKind.MARKDOWN_AGENT, role="provider-watcher")
    tool_name, args = _route_for_kind(record)
    assert tool_name == "invoke_agent"
    assert args["name"] == "provider-watcher"
