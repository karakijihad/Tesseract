"""AU-16 S2 — three derived trees built from the leaf stream.

Each tree is operator-readable markdown under
``<TESSERACT_HOME>/memory-store/trees/{source,topic,global}/``.

- ``source_tree`` — one file per source slug; newest seal section first.
- ``topic_tree`` — one file per activated entity; lazy instantiation
  once the entity is referenced ≥``TOPIC_ACTIVATION_THRESHOLD`` times.
- ``global_tree`` — one file per UTC date; daily roll-up of every seal.

All three derive from ``Seal`` artefacts produced by ``SealJob`` (S1).
Trees never write outside their own subdir; the vault stays untouched.
"""

from tesseract.memory.trees.source_tree import (
    SOURCE_TREES_ROOT,
    list_source_tree_paths,
    read_source_tree,
    source_tree_path,
    write_seal_section,
)
from tesseract.memory.trees.topic_tree import (
    TOPIC_ACTIVATION_THRESHOLD,
    TOPIC_TREES_ROOT,
    activate_topic,
    is_topic_active,
    list_active_topics,
    topic_tree_path,
)
from tesseract.memory.trees.global_tree import (
    GLOBAL_TREES_ROOT,
    daily_digest_path,
    list_digest_dates,
    read_daily_digest,
    write_daily_digest,
)


__all__ = [
    "GLOBAL_TREES_ROOT",
    "SOURCE_TREES_ROOT",
    "TOPIC_ACTIVATION_THRESHOLD",
    "TOPIC_TREES_ROOT",
    "activate_topic",
    "daily_digest_path",
    "is_topic_active",
    "list_active_topics",
    "list_digest_dates",
    "list_source_tree_paths",
    "read_daily_digest",
    "read_source_tree",
    "source_tree_path",
    "topic_tree_path",
    "write_daily_digest",
    "write_seal_section",
]
