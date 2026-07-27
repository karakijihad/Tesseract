"""Knowledge-keeper → yaml_change_proposal emitter.

After ``provider_watch`` writes ``vault/knowledge-base/providers/<provider>.md``,
this module diffs the file's ``canonical_models`` frontmatter against the
matching ``providers.yaml::api.<provider>.models`` block and emits a
``yaml_change_proposal`` workspace event per detected delta. Emit-time
dedup honors :meth:`EventStore.has_pending_yaml_proposal`.

v1 honest scope: ``canonical_models`` in the KB is operator/agent-populated,
so emits are quiet until the structured surface is filled. The emit path
is wired and tested so MO-10-2's downstream apply path has something to
feed on.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

import yaml

from tesseract.knowledge_keeper.content_merge import split_frontmatter
from tesseract.workspace_events import EventStore, WorkspaceEvent
from tesseract.workspace_events.yaml_change_proposal import (
    KindOrigin,
    YamlChangeProposalPayload,
)

log = logging.getLogger(__name__)


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _kb_models(target: Path) -> dict[str, dict[str, Any]]:
    if not target.exists():
        return {}
    fm, _body = split_frontmatter(target.read_text(encoding="utf-8"))
    entries = fm.get("canonical_models") or []
    if not isinstance(entries, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("id") or "").strip()
        if not mid:
            continue
        out[mid] = entry
    return out


def _catalog_models(providers_yaml: Path, provider_slug: str) -> dict[str, dict[str, Any]]:
    if not providers_yaml.exists():
        return {}
    with providers_yaml.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    api = doc.get("api") or {}
    if not isinstance(api, dict):
        return {}
    # KB slug maps to lower-case provider key. NIM has slug "nvidia-nim"
    # but YAML key "nim" — normalize.
    yaml_key = {"nvidia-nim": "nim"}.get(provider_slug, provider_slug)
    prov = api.get(yaml_key)
    if not isinstance(prov, dict):
        return {}
    models = prov.get("models") or {}
    if not isinstance(models, dict):
        return {}
    return models


def _summary(kind_origin: KindOrigin, provider: str, model_id: str) -> str:
    label = kind_origin.replace("_", " ")
    return f"{provider}: {label} — {model_id}"


def emit_proposals_for_provider(
    *,
    kb_file: Path,
    provider_slug: str,
    providers_yaml: Path,
    event_store: EventStore,
) -> list[str]:
    """Diff the KB file vs ``providers.yaml`` and emit per-delta proposals.

    Returns the list of ``event_id`` values for emitted events. An empty
    list means either no deltas were found or every candidate was deduped.
    """
    if not kb_file.exists() or not providers_yaml.exists():
        return []

    kb = _kb_models(kb_file)
    catalog = _catalog_models(providers_yaml, provider_slug)
    if not kb:
        return []

    yaml_key = {"nvidia-nim": "nim"}.get(provider_slug, provider_slug)
    target_path = "tesseract/config/providers.yaml"
    file_hash = _hash_file(providers_yaml)
    file_size = providers_yaml.stat().st_size

    emitted: list[str] = []
    for model_id, entry in kb.items():
        if model_id not in catalog:
            yaml_path = f"api.{yaml_key}.models.{model_id}"
            if event_store.has_pending_yaml_proposal(
                target_path=target_path,
                yaml_path=yaml_path,
                kind_origin="provider_model_added",
            ):
                continue
            content = {k: v for k, v in entry.items() if k != "id"}
            payload = YamlChangeProposalPayload(
                target_path=target_path,
                action="insert_under_path",
                yaml_path=yaml_path,
                content=content,
                summary=_summary("provider_model_added", provider_slug, model_id),
                diff="",
                kind_origin="provider_model_added",
                expected_hash_before=file_hash,
                bytes_before=file_size,
                bytes_after=file_size,
            )
            emitted.append(_emit_event(event_store, payload))
            continue

        # Pricing / context-window / deprecation diffs — compare scalar
        # fields per the kind_origin Literal.
        current = catalog[model_id]
        for field, origin in (
            ("pricing_input_per_mtok_usd", "provider_pricing_changed"),
            ("pricing_output_per_mtok_usd", "provider_pricing_changed"),
            ("context_window", "provider_context_changed"),
            ("status", "provider_model_deprecated"),
        ):
            kb_val = entry.get(field)
            cat_val = current.get(field)
            if kb_val is None or kb_val == cat_val:
                continue
            yaml_path = f"api.{yaml_key}.models.{model_id}.{field}"
            if event_store.has_pending_yaml_proposal(
                target_path=target_path,
                yaml_path=yaml_path,
                kind_origin=origin,
            ):
                continue
            payload = YamlChangeProposalPayload(
                target_path=target_path,
                action="update_field",
                yaml_path=yaml_path,
                content=kb_val,
                summary=_summary(origin, provider_slug, model_id),  # type: ignore[arg-type]
                diff="",
                kind_origin=origin,  # type: ignore[arg-type]
                expected_hash_before=file_hash,
                bytes_before=file_size,
                bytes_after=file_size,
            )
            emitted.append(_emit_event(event_store, payload))
    return emitted


def _emit_event(store: EventStore, payload: YamlChangeProposalPayload) -> str:
    ev = WorkspaceEvent.new(
        kind="yaml_change_proposal",
        source="knowledge_keeper",
        title=payload.summary,
        summary=payload.summary,
        payload=payload.model_dump(),
        priority=6,
        author_id="system",
        author_display="Knowledge keeper",
    )
    appended = store.append_event(ev)
    return appended.event_id


__all__ = ["emit_proposals_for_provider"]
