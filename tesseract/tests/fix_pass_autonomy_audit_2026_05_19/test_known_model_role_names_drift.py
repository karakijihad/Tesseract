"""Pin _KNOWN_MODEL_ROLE_NAMES against the live roles.yaml.

Codex audit 2026-05-19 P0 #1 was caused by autonomy storing a *model
role* name ("agents_default") in the *agent slug* field. The runner's
defence is a hardcoded frozenset of known role names; if roles.yaml
gains a new role and this set isn't updated, the same bug silently
returns for that role.

This test asserts every active role name in roles.yaml is present in
the runner's defensive set. Operator adds a role → test fails →
operator updates the set in the same commit.
"""

from __future__ import annotations

import pytest
from ruamel.yaml import YAML

from tesseract.orchestrator.autonomy.kernel_worker_runner import (
    _KNOWN_MODEL_ROLE_NAMES,
)
from tesseract.config.loader import ROLES_YAML


def _load_role_names() -> set[str]:
    yaml = YAML(typ="safe")
    with open(ROLES_YAML, "r", encoding="utf-8") as f:
        raw = yaml.load(f)
    names: set[str] = set()
    # Top-level non-roles surface keys that still resolve as model
    # *role* names elsewhere in the codebase: ``embeddings``, the
    # voice sub-roles, etc. Keep this in lockstep with the runner
    # set.
    if isinstance(raw.get("embeddings"), dict):
        names.add("embeddings")
    voice = raw.get("voice") or {}
    if isinstance(voice, dict):
        for key in ("stt", "tts"):
            if key in voice:
                names.add(key)
    roles_block = raw.get("roles") or {}
    if isinstance(roles_block, dict):
        names.update(roles_block.keys())
    return names


def test_known_model_role_names_covers_live_roles_yaml() -> None:
    """Every name under ``roles.yaml::roles`` (plus the top-level
    ``embeddings`` + ``voice.{stt,tts}``) must appear in the runner's
    defensive frozenset. Otherwise a new role can silently land back
    in ``WorkerRecord.role`` and reproduce the audit P0 #1 bug."""
    live = _load_role_names()
    missing = live - _KNOWN_MODEL_ROLE_NAMES
    assert not missing, (
        f"_KNOWN_MODEL_ROLE_NAMES is missing live role names: {sorted(missing)}. "
        "Add them to the set in tesseract/orchestrator/autonomy/kernel_worker_runner.py "
        "or this role will silently break worker dispatch when stored as record.role."
    )
