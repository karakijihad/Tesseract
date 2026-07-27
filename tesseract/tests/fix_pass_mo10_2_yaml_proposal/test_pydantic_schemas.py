"""MO-10-2 §2a — Pydantic schemas validate the live catalog cleanly."""

from __future__ import annotations

import yaml

from tesseract.config._schemas import ProvidersConfig, RolesConfig
from tesseract.paths import TESSERACT_DIR


def test_providers_yaml_validates():
    raw = yaml.safe_load((TESSERACT_DIR / "config" / "providers.yaml").read_text(encoding="utf-8"))
    ProvidersConfig.model_validate(raw)


def test_roles_yaml_validates():
    raw = yaml.safe_load((TESSERACT_DIR / "config" / "roles.yaml").read_text(encoding="utf-8"))
    RolesConfig.model_validate(raw)


def test_providers_rejects_unknown_top_level_key():
    raw = {"availability": {}, "chain": {}, "cost_tracking": {}, "api": {}, "cli": {}, "local": {}, "garbage": True}
    try:
        ProvidersConfig.model_validate(raw)
    except Exception:
        return
    raise AssertionError("expected validation failure on unknown top-level key")


def test_roles_rejects_missing_embeddings():
    raw = {"roles": {}}
    try:
        RolesConfig.model_validate(raw)
    except Exception:
        return
    raise AssertionError("expected validation failure when embeddings missing")
