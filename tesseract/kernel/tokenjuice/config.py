"""TokenJuice config + path resolution.

`load_config()` reads `tesseract/config/tokenjuice.yaml`; every key is
required (no silent `.get(..., default)` per CLAUDE.md hard rules).
`user_rules_dir()` and `project_rules_dir()` resolve TESSERACT_HOME / ROOT
at call time so test fixtures stay isolated.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from tesseract.paths import CONFIG_DIR, ROOT, TESSERACT_DIR, TESSERACT_HOME

TOKENJUICE_YAML = CONFIG_DIR / "tokenjuice.yaml"
BUILTIN_RULES_DIR = TESSERACT_DIR / "kernel" / "tokenjuice" / "builtin"


@dataclass(frozen=True)
class TokenJuiceConfig:
    enabled: bool
    dry_run: bool
    audit_log: bool
    disabled_rules: dict[str, list[str]]


def _require(d: dict, key: str, where: str):
    if key not in d:
        raise RuntimeError(f"missing required key '{key}' in {where}")
    return d[key]


def load_config(path: Path | None = None) -> TokenJuiceConfig:
    p = path or TOKENJUICE_YAML
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"{p.name} must be a mapping")
    disabled_raw = _require(raw, "disabled_rules", p.name) or {}
    if not isinstance(disabled_raw, dict):
        raise RuntimeError(f"{p.name}::disabled_rules must be a mapping")
    return TokenJuiceConfig(
        enabled=bool(_require(raw, "enabled", p.name)),
        dry_run=bool(_require(raw, "dry_run", p.name)),
        audit_log=bool(_require(raw, "audit_log", p.name)),
        disabled_rules={k: list(v or []) for k, v in disabled_raw.items()},
    )


def user_rules_dir() -> Path:
    override = os.environ.get("TESSERACT_HOME")
    return (Path(override).resolve() if override else TESSERACT_HOME) / "tokenjuice" / "user"


def project_rules_dir() -> Path:
    return ROOT / ".tokenjuice"
