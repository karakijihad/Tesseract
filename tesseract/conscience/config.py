"""Drift-config loader for tesseract/config/conscience.yaml.

Follows the same contract as `brain/boot.py` — raises `RuntimeError` on any
missing required key. No `.get(..., default)` for infrastructure values.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from tesseract.paths import CONFIG_DIR

CONSCIENCE_YAML = CONFIG_DIR / "conscience.yaml"


def _require(d: dict, key: str, where: str):
    if key not in d:
        raise RuntimeError(f"missing required key '{key}' in {where}")
    return d[key]


@dataclass(frozen=True)
class DriftConfig:
    window_hours: int
    thresholds: dict[str, dict[str, float]]


def load_drift_config(path: Path = CONSCIENCE_YAML) -> DriftConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    drift = _require(raw, "drift", path.name)
    window_hours = int(_require(drift, "window_hours", f"{path.name} drift"))
    signals = _require(drift, "signals", f"{path.name} drift")
    thresholds: dict[str, dict[str, float]] = {}
    for name, block in signals.items():
        where = f"{path.name} drift.signals.{name}"
        thresholds[name] = {
            "warn": float(_require(block, "warn", where)),
            "bad": float(_require(block, "bad", where)),
        }
    return DriftConfig(window_hours=window_hours, thresholds=thresholds)
