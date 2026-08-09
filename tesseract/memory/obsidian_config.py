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

# Vault palette — same three-band scheme, keyed to what vault pages actually
# carry: operator-curated entity hubs are typed (Concept/Person/...), compiled
# Source pages carry the generated `source` tag, bookkeeping pages match by
# filename. The memory-store tag groups above never fire on vault pages,
# which used to leave the whole vault graph default-grey.
VAULT_COLOR_GROUPS: list[dict] = [
    # Hubs — red #e0524f
    {"query": '["type":Concept]', "color": {"a": 1, "rgb": 14701135}},
    {"query": '["type":Person]', "color": {"a": 1, "rgb": 14701135}},
    {"query": '["type":Project]', "color": {"a": 1, "rgb": 14701135}},
    {"query": '["type":Tool]', "color": {"a": 1, "rgb": 14701135}},
    {"query": '["type":Organization]', "color": {"a": 1, "rgb": 14701135}},
    # Compiled sources — yellow #e8a02c
    {"query": "tag:#source", "color": {"a": 1, "rgb": 15245356}},
    {"query": '["type":Source]', "color": {"a": 1, "rgb": 15245356}},
    # Bookkeeping — orange #d97757
    {"query": "file:INDEX", "color": {"a": 1, "rgb": 14251863}},
    {"query": "file:TAXONOMY", "color": {"a": 1, "rgb": 14251863}},
    {"query": "file:LINT-REPORT", "color": {"a": 1, "rgb": 14251863}},
    {"query": "file:ingest-log", "color": {"a": 1, "rgb": 14251863}},
]


def graph_json_path(root: Path) -> Path:
    return root / ".obsidian" / "graph.json"


def ensure_obsidian_config(root: Path, color_groups: list[dict] | None = None) -> Path:
    """Write the graph config at ``<root>/.obsidian/graph.json``.

    ``color_groups`` selects the palette (default: the memory-store groups).
    Four cases:

    - **Missing file** — write the full default. Fresh-checkout case.
    - **Empty stub** — Obsidian writes a ``colorGroups: []`` stub the
      first time the operator opens the vault. Without intervention
      the operator would never see colours because the file *exists*
      but carries no palette. We detect the empty-stub shape and merge
      our groups in, preserving every other field Obsidian set
      (showTags, scale, layout, etc.).
    - **Shipped-default palette** — the file carries exactly a palette we
      shipped (e.g. the vault received the memory-store groups verbatim
      before it had its own). That is our artifact, not operator
      customisation — upgrade it to the requested palette in place.
    - **Operator-customised** (any other non-empty ``colorGroups``) —
      leave it alone. Genuine customisation survives every boot.
    """
    groups = color_groups if color_groups is not None else GRAPH_JSON_DEFAULT["colorGroups"]
    target = graph_json_path(root)
    if not target.exists():
        doc = dict(GRAPH_JSON_DEFAULT)
        doc["colorGroups"] = groups
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        log.info("obsidian config seeded at %s", target)
        return target
    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("obsidian config at %s unreadable — leaving alone", target)
        return target
    if not isinstance(existing, dict):
        return target
    current = existing.get("colorGroups")
    shipped_palettes = (GRAPH_JSON_DEFAULT["colorGroups"], VAULT_COLOR_GROUPS)
    if current and current != groups and current in shipped_palettes:
        upgraded = dict(existing)
        upgraded["colorGroups"] = groups
        target.write_text(json.dumps(upgraded, indent=2), encoding="utf-8")
        log.info("obsidian config palette upgraded at %s", target)
        return target
    if current:
        return target  # operator customised — do not touch
    merged = dict(existing)
    merged["colorGroups"] = groups
    target.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    log.info("obsidian config color groups merged into %s", target)
    return target


__all__ = [
    "GRAPH_JSON_DEFAULT",
    "VAULT_COLOR_GROUPS",
    "ensure_obsidian_config",
    "graph_json_path",
]
