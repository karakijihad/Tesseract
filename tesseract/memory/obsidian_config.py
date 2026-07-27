"""AU-16 — shipped ``.obsidian/graph.json`` for the canonical stores.

Operator opens ``tesseract/memory-store/`` (and optionally
``tesseract/vault/``) directly as Obsidian vaults. The shipped color
groups apply on first open — no separate mirror directory.

Unified palette (operator-approved 2026-05-19):

- **Red hub** (``#e0524f``)
  - ``#topic-summary`` — AU-16 entity-keyed aggregator trees
  - ``#feedback``      — legacy operator rules (govern behavior)

- **Yellow rollup** (``#e8a02c``)
  - ``#source-summary``, ``#global-digest`` — AU-16 sealed rollups
  - ``#user``, ``#project``, ``#daily-note`` — legacy durable knowledge

- **Orange in-flight** (``#d97757``)
  - ``#pending``, ``#buffered`` — AU-16 in-flight leaves
  - ``#conscience``             — legacy runtime drift telemetry

- **Default grey** — everything else (``#leaf``, ``#reference``, …)

``ensure_obsidian_config(root)`` writes the default only when the file
is missing, so operator customisations in Obsidian's UI survive.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


GRAPH_JSON_DEFAULT: dict = {
    "collapse-filter": True,
    "search": "",
    "showTags": True,
    "showAttachments": False,
    "hideUnresolved": False,
    "showOrphans": True,
    "collapse-color-groups": False,
    "colorGroups": [
        # Hub — red #e0524f → 0xE0524F = 14701135
        {"query": "tag:#topic-summary", "color": {"a": 1, "rgb": 14701135}},
        {"query": "tag:#feedback", "color": {"a": 1, "rgb": 14701135}},
        # Rollup — yellow #e8a02c → 0xE8A02C = 15245356
        {"query": "tag:#global-digest", "color": {"a": 1, "rgb": 15245356}},
        {"query": "tag:#source-summary", "color": {"a": 1, "rgb": 15245356}},
        {"query": "tag:#user", "color": {"a": 1, "rgb": 15245356}},
        {"query": "tag:#project", "color": {"a": 1, "rgb": 15245356}},
        {"query": "tag:#daily-note", "color": {"a": 1, "rgb": 15245356}},
        # In-flight — orange #d97757 → 0xD97757 = 14251863
        {"query": "tag:#pending", "color": {"a": 1, "rgb": 14251863}},
        {"query": "tag:#buffered", "color": {"a": 1, "rgb": 14251863}},
        {"query": "tag:#conscience", "color": {"a": 1, "rgb": 14251863}},
    ],
    "collapse-display": False,
    "showArrow": False,
    "textFadeMultiplier": 0,
    "nodeSizeMultiplier": 1,
    "lineSizeMultiplier": 1,
    "collapse-forces": False,
    "centerStrength": 0.518713248970312,
    "repelStrength": 10,
    "linkStrength": 1,
    "linkDistance": 250,
    "scale": 1,
    "close": False,
}


def graph_json_path(root: Path) -> Path:
    return root / ".obsidian" / "graph.json"


def ensure_obsidian_config(root: Path) -> Path:
    """Write ``GRAPH_JSON_DEFAULT`` at ``<root>/.obsidian/graph.json``.

    Three cases:

    - **Missing file** — write the full default. Fresh-checkout case.
    - **Empty stub** — Obsidian writes a ``colorGroups: []`` stub the
      first time the operator opens the vault. Without intervention
      the operator would never see colours because the file *exists*
      but carries no palette. We detect the empty-stub shape and merge
      our default groups in, preserving every other field Obsidian
      set (showTags, scale, layout, etc.).
    - **Operator-customised** (``colorGroups`` non-empty) — leave it
      alone. Genuine customisation survives every boot.
    """
    target = graph_json_path(root)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(GRAPH_JSON_DEFAULT, indent=2), encoding="utf-8")
        log.info("obsidian config seeded at %s", target)
        return target
    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("obsidian config at %s unreadable — leaving alone", target)
        return target
    if not isinstance(existing, dict):
        return target
    if existing.get("colorGroups"):
        return target  # operator customised — do not touch
    merged = dict(existing)
    merged["colorGroups"] = GRAPH_JSON_DEFAULT["colorGroups"]
    target.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    log.info("obsidian config color groups merged into %s", target)
    return target


__all__ = ["GRAPH_JSON_DEFAULT", "ensure_obsidian_config", "graph_json_path"]
