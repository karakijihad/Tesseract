"""Knowledge-keeper writer — vault/knowledge-base/ refresh discipline.

Operator-local knowledge surface. ``cli-reference`` and other agents
read these files for current state of providers, CLIs, and the agent
ecosystem. Distinct from the catalog (``providers.yaml`` /
``roles.yaml``) which is ASK-gated config truth.

Three concerns live here:

1. ``scaffolding`` — boot-time tree creation (idempotent).
2. ``content_merge`` — three-way merge so refresher writes don't clobber
   operator hand-edits.
3. ``refresh_log`` — per-subdir JSONL append for "what got refreshed when".

The actual refresh source (Tavily, scraped GitHub releases) lives in
the scheduler task — ``scheduler/tasks/provider_watch.py``. This
package is the shared substrate.
"""

from tesseract.knowledge_keeper.content_merge import (
    MergeConflict,
    MergeResult,
    merge_kb_file,
    split_frontmatter,
)
from tesseract.knowledge_keeper.refresh_log import append_refresh_row
from tesseract.knowledge_keeper.scaffolding import (
    KB_SUBDIRS,
    ensure_kb_tree,
    kb_root,
)

__all__ = [
    "KB_SUBDIRS",
    "MergeConflict",
    "MergeResult",
    "append_refresh_row",
    "ensure_kb_tree",
    "kb_root",
    "merge_kb_file",
    "split_frontmatter",
]
