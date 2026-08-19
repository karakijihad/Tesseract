"""The atlas — one derived map over memory, vault and runtime.

Two retrieval paths exist today and they do not merge: `memory_search` over the
memory store, `vault_query` / `vault_search` over the wiki. An agent asking a
question gets two answers and can trace neither. This is the join.

Everything here is derived and disposable. The builder writes only under
`<TESSERACT_HOME>/atlas/`, so a bad build costs one rebuild and never data —
that is what makes it safe to keep improving the extractor.
"""

from tesseract.orchestrator.atlas.build import BUILDER_VERSION, BuildReport, run_build
from tesseract.orchestrator.atlas.model import (
    PRECEDENCE,
    Creator,
    Edge,
    Node,
    NodeKind,
    Provenance,
    entity_id,
)
from tesseract.orchestrator.atlas.store import Atlas, atlas_dir, atlas_path

__all__ = [
    "BUILDER_VERSION",
    "PRECEDENCE",
    "Atlas",
    "BuildReport",
    "Creator",
    "Edge",
    "Node",
    "NodeKind",
    "Provenance",
    "atlas_dir",
    "atlas_path",
    "entity_id",
    "run_build",
]
