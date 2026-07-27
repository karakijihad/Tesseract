"""Payload shape for ``yaml_change_proposal`` workspace events (MO-10-2).

Carried inside ``WorkspaceEvent.payload`` so the apply path
(``kernel.workspace_changes``) can read it back without parsing free
text. Pydantic v2 model — validation on emit + on apply.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


YamlAction = Literal[
    "insert_under_path",
    "update_field",
    "append_to_list_at_path",
]


KindOrigin = Literal[
    "provider_model_added",
    "provider_pricing_changed",
    "provider_context_changed",
    "provider_model_deprecated",
    "role_model_remapped",
]


class YamlChangeProposalPayload(BaseModel):
    """Payload shape for ``yaml_change_proposal`` events.

    ``content`` is the raw value to apply at ``yaml_path`` — a scalar for
    ``update_field``, a mapping for ``insert_under_path``, or any
    serializable for ``append_to_list_at_path``. ``expected_hash_before``
    pins the pre-edit sha256 so concurrent operator edits drop the
    proposal cleanly rather than silently clobber.
    """

    model_config = ConfigDict(extra="forbid")

    target_path: str = Field(description="Repo-relative YAML path, e.g. tesseract/config/providers.yaml")
    action: YamlAction
    yaml_path: str = Field(description="Dotted path inside the YAML doc, e.g. api.anthropic.models.claude_4_8")
    content: Any
    summary: str = Field(max_length=400)
    diff: str = Field(default="", description="Unified diff of file before/after — preview only")
    kind_origin: KindOrigin
    expected_hash_before: str = Field(min_length=64, max_length=64)
    bytes_before: int = 0
    bytes_after: int = 0


__all__ = [
    "KindOrigin",
    "YamlAction",
    "YamlChangeProposalPayload",
]
