"""vault_query tool — query the vault wiki knowledge base.

Reads vault/wiki/INDEX.md first (topic overview), then walks linked wiki pages
that match the query, and synthesizes an answer via VaultLibrarian.

Use when:
- "Do we have research on X?"
- "What does our vault say about Y?"
- "Find the paper about Z from two weeks ago"
- "Which of our documents connect to topic W?"

Distinct from vault_search: vault_search scans raw file chunks (BM25/vector).
vault_query reads the compiled wiki pages (structured summaries with links).

Scoping (Phase 4 of vault-librarian-rewire) is a deterministic two-pass
traversal over the compound wiki Phase 3 builds. Pass 1 (seed) keyword-matches
INDEX.md + per-page `title`/`concepts` frontmatter. Pass 2 (expand) unions each
seed's `related_slugs:` + `backlinks_from:`, dedupes, and caps. Scoping is
token-free; `VaultLibrarian.synthesize_query()` reasons over the expanded set.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.memory.vault_manager import VaultManager

if TYPE_CHECKING:
    from tesseract.brain.boot import VaultConfig
    from tesseract.memory.vault_librarian import VaultLibrarian


class VaultQueryInput(BaseModel):
    query: str = Field(description="Question or topic to look up in the vault wiki")
    topic_filter: str | None = Field(
        default=None,
        description="Optional: limit to a specific topic slug (e.g. 'system-dynamics')",
    )


_STOP_WORDS = frozenset({"the", "a", "an", "is", "are", "do", "we", "have", "what"})

# Emitted after a partial page, and reserved before the slice is taken.
_TRUNCATION_SUFFIX = "\n…[truncated]\n"
# Below this much room a partial page is noise, so the slot is spent on saying
# the budget ran out instead.
_MIN_EXCERPT_CHARS = 200


class VaultQueryTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"
    # Audit-3 M9 — wiki + raw vault content is operator-curated but can
    # still ingest third-party documents that carry prompt-injection
    # payloads. Wrap in the UNTRUSTED_TOOL_OUTPUT envelope so the model
    # treats vault snippets as data, not instructions.
    untrusted_source: ClassVar[bool] = True

    def __init__(
        self,
        vault_manager: VaultManager,
        vault_config: "VaultConfig",
        vault_librarian: "VaultLibrarian | None" = None,
    ) -> None:
        self._manager = vault_manager
        self._config = vault_config
        self._librarian = vault_librarian

    @property
    def name(self) -> str:
        return "vault_query"

    @property
    def description(self) -> str:
        return (
            "Query the vault's compiled wiki knowledge base. Use for questions about what research "
            "or documents we have collected, or to retrieve information from past uploads. "
            "Reads topic-grouped wiki summaries with inter-source links. "
            "Use vault_search for raw full-text keyword search."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return VaultQueryInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = tool_input if isinstance(tool_input, VaultQueryInput) else VaultQueryInput(**tool_input.model_dump())

        index_content = self._manager.read_wiki_index()
        page_count = _count_wiki_entries(index_content)
        if not index_content.strip() or page_count == 0:
            return ToolResult(
                output="The vault wiki is empty. No sources have been compiled yet. Use vault_ingest to add sources to vault/raw/."
            )

        seeds, expanded = _scope_candidates(
            self._manager, self._config, index_content, inp.query, inp.topic_filter,
        )

        all_topics = _list_topics(index_content)
        topic_summary = ", ".join(all_topics) if all_topics else "none"

        if not seeds and not expanded:
            return ToolResult(
                output=(
                    f"Vault wiki has {page_count} page(s) across topics: {topic_summary}\n\n"
                    f"No pages found matching your query. Try vault_search for raw text search, "
                    f"or browse topics: {topic_summary}"
                )
            )

        candidate_slugs = seeds + expanded
        header = (
            f"Vault wiki: {page_count} page(s) · Topics: {topic_summary}\n"
            f"Matched pages: seed={_fmt_slugs(seeds)}, expanded={_fmt_slugs(expanded)}\n"
        )

        if self._librarian is not None:
            synthesized = await self._librarian.synthesize_query(inp.query, candidate_slugs)
            return ToolResult(output=f"{header}\n{synthesized}")

        body = _render_raw_pages(
            self._manager, candidate_slugs, self._config.synthesis_char_budget
        )
        return ToolResult(output=f"{header}\n{body}")


def _fmt_slugs(slugs: list[str]) -> str:
    return "[" + ", ".join(slugs) + "]"


def _list_topics(index_content: str) -> list[str]:
    topics = []
    for line in index_content.splitlines():
        if line.startswith("## "):
            topics.append(line[3:].strip())
    return topics


def _count_wiki_entries(index_content: str) -> int:
    return sum(1 for line in index_content.splitlines() if line.strip().startswith("- [["))


def _query_tokens(query: str) -> set[str]:
    words = set(re.split(r"\W+", query.lower())) - _STOP_WORDS
    return {w for w in words if len(w) >= 2}


def _scope_candidates(
    manager: VaultManager,
    config: "VaultConfig",
    index_content: str,
    query: str,
    topic_filter: str | None,
) -> tuple[list[str], list[str]]:
    """Two-pass deterministic scoping over the compound wiki.

    Pass 1 (seed): keyword match INDEX.md sections + per-page frontmatter
    title/concepts. Pass 2 (expand): union related_slugs + backlinks_from
    of each seed. Returns (seeds, expanded) — both in traversal order.
    """
    tokens = _query_tokens(query)
    seeds = _seed_pass(manager, index_content, tokens, topic_filter, config.max_seed_slugs)
    expanded = _expand_pass(manager, seeds, config.max_expanded_slugs)
    return seeds, expanded


def _seed_pass(
    manager: VaultManager,
    index_content: str,
    tokens: set[str],
    topic_filter: str | None,
    cap: int,
) -> list[str]:
    """Keyword match over INDEX.md + per-page title/concepts frontmatter."""
    seeds: list[str] = []
    seen: set[str] = set()
    current_topic = ""
    current_topic_slug = ""

    for line in index_content.splitlines():
        if line.startswith("## "):
            current_topic = line[3:].strip()
            current_topic_slug = _slugify_simple(current_topic)
            continue
        if not line.strip().startswith("- [["):
            continue
        if topic_filter and current_topic_slug != topic_filter:
            continue

        slug_match = re.search(r"\[\[([^\]]+)\]\]", line)
        if not slug_match:
            continue
        slug = slug_match.group(1)
        if slug in seen:
            continue

        index_text = (current_topic + " " + line).lower()
        fm_text = _frontmatter_match_text(manager, slug)
        combined = f"{index_text} {fm_text}"
        topic_match = bool(topic_filter) and current_topic_slug == topic_filter
        token_match = any(t in combined for t in tokens) if tokens else False

        if topic_match or token_match:
            seeds.append(slug)
            seen.add(slug)
            if len(seeds) >= cap:
                break
    return seeds


def _expand_pass(
    manager: VaultManager,
    seeds: list[str],
    cap: int,
) -> list[str]:
    """Union each seed's related_slugs + backlinks_from; dedupe; cap."""
    seed_set = set(seeds)
    expanded: list[str] = []
    seen: set[str] = set(seed_set)

    for seed in seeds:
        fm = manager.read_wiki_page_frontmatter(seed)
        linked = [str(s) for s in (fm.get("related_slugs") or [])]
        linked += [str(s) for s in (fm.get("backlinks_from") or [])]
        for slug in linked:
            if not slug or slug in seen:
                continue
            expanded.append(slug)
            seen.add(slug)
            if len(expanded) >= cap:
                return expanded
    return expanded


def _frontmatter_match_text(manager: VaultManager, slug: str) -> str:
    """Return a lowercase string of per-page title + concepts for keyword match."""
    fm = manager.read_wiki_page_frontmatter(slug)
    if not fm:
        return ""
    pieces: list[str] = []
    title = fm.get("title")
    if isinstance(title, str):
        pieces.append(title)
    for c in fm.get("concepts") or []:
        pieces.append(str(c))
    return " ".join(pieces).lower()


def _slugify_simple(text: str) -> str:
    # Delegate to the canonical vault slugifier (audit-1 (2026-04-24) M7).
    # The prior local version skipped NFKD and the length cap, so lookups
    # for accented or long topic strings could miss matching wiki pages.
    from tesseract.memory.vault_manager import slugify
    return slugify(text)


def _render_raw_pages(manager: VaultManager, slugs: list[str], budget: int) -> str:
    """Render whole wiki pages under one character budget.

    Every emitted block is charged, not just page bodies: the missing-page
    marker, the truncation notice, the trailing newline and the join separator
    all reach the caller, and counting only headers and bodies let the result
    run past the cap `vault.yaml` says bounds the assembled whole.
    """
    blocks: list[str] = []
    used = 0

    def _sep() -> int:
        return 1 if blocks else 0  # the "\n" this block's join will add

    def _fits(block: str) -> bool:
        return used + _sep() + len(block) <= budget

    def _charge(block: str) -> None:
        nonlocal used
        used += _sep() + len(block)
        blocks.append(block)

    for slug in slugs:
        page = manager.read_wiki_page(slug)
        header = f"=== {slug} ===\n"
        if page is None:
            block = f"{header}(missing)\n"
            if not _fits(block):
                break
            _charge(block)
            continue

        # Build the exact string first, then ask whether it fits. Deriving a
        # slice length from the budget and appending decoration afterwards is
        # what let the earlier version overshoot: the truncation suffix and the
        # join separator were emitted but never reserved.
        whole = f"{header}{page}\n"
        if _fits(whole):
            _charge(whole)
            continue

        room = budget - used - _sep() - len(header) - len(_TRUNCATION_SUFFIX)
        if room <= _MIN_EXCERPT_CHARS:
            notice = f"{header}[truncated — page skipped, budget exhausted]\n"
            if _fits(notice):
                _charge(notice)
            break
        _charge(f"{header}{page[:room]}{_TRUNCATION_SUFFIX}")
        break
    return "\n".join(blocks)
