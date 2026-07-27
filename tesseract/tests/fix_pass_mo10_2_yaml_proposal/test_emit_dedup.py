"""MO-10-2 §2f — emit-time dedup helper on EventStore."""

from __future__ import annotations

from tesseract.workspace_events import EventStore, WorkspaceEvent


def test_has_pending_yaml_proposal_matches_triple(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    store = EventStore(tmp_path / "logs")
    ev = WorkspaceEvent.new(
        kind="yaml_change_proposal",
        source="knowledge_keeper",
        title="Anthropic — model added",
        summary="add claude_4_8",
        payload={
            "target_path": "tesseract/config/providers.yaml",
            "yaml_path": "api.anthropic.models.claude_4_8",
            "kind_origin": "provider_model_added",
        },
        priority=6,
        author_id="system",
        author_display="Knowledge keeper",
    )
    store.append_event(ev)
    assert store.has_pending_yaml_proposal(
        target_path="tesseract/config/providers.yaml",
        yaml_path="api.anthropic.models.claude_4_8",
        kind_origin="provider_model_added",
    )
    assert not store.has_pending_yaml_proposal(
        target_path="tesseract/config/providers.yaml",
        yaml_path="api.anthropic.models.other",
        kind_origin="provider_model_added",
    )
    # Once flipped to applied, the dedup helper stops considering it
    # pending.
    store.update_event_status(ev.event_id, "applied")
    assert not store.has_pending_yaml_proposal(
        target_path="tesseract/config/providers.yaml",
        yaml_path="api.anthropic.models.claude_4_8",
        kind_origin="provider_model_added",
    )
