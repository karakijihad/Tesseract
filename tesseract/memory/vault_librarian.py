"""Vault librarian — compiles raw vault sources into wiki pages.

For each file added to vault/raw/, the librarian:
1. Extracts text via VaultIndexer.extract_text()
2. Calls the vault-librarian agent's configured model to produce JSON metadata (topic, summary, entities, etc.)
3. Writes a structured wiki page to vault/wiki/{slug}.md
4. Updates vault/wiki/INDEX.md with the topic section entry
5. Appends to vault/wiki/ingest-log.md

Also provides synthesize_query() for vault_query tool answers.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from tesseract.agents.loader import AgentDefinition, load_agent
from tesseract.context.circuit_breaker import CircuitBreaker
from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.memory.vault_indexer import VaultIndexer
from tesseract.memory.vault_manager import VaultManager, slugify

if TYPE_CHECKING:
    from tesseract.brain.boot import VaultConfig

logger = logging.getLogger(__name__)

_EMPTY_SOURCE_TOPIC = "general"
_EMPTY_SOURCE_SUMMARY = "(no extractable text)"


class VaultContainmentError(ValueError):
    """Raised when a raw_rel_path resolves outside vault/raw/."""


@dataclass(frozen=True)
class VaultWikiPage:
    slug: str
    title: str
    topic: str
    summary: str
    entities: list[str]
    concepts: list[str]
    open_questions: list[str]
    related_slugs: list[str]
    source_path: str
    date_added: str
    type: str = "Source"
    backlinks_from: list[str] = field(default_factory=list)
    # sha256 of the raw source bytes at compile time — lets a changed
    # source recompile instead of being skipped as already-compiled.
    source_hash: str = ""


_slugify = slugify  # legacy alias — call sites below predate the manager-level helper


def _parse_llm_json(raw: str) -> dict:
    """Extract and parse the first JSON object from an LLM response."""
    # Strip any surrounding text / markdown fences
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        logger.warning("VaultLibrarian: failed to parse LLM JSON response")
        return {}


# A scalar safe to emit bare: no YAML indicator can start it and nothing in it
# can open a nested structure. Anything else gets quoted rather than reshaped,
# so the page still says what the model said.
_PLAIN_FM_SCALAR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._/+()'-]*$")


def _fm_scalar(value: str) -> str:
    """One frontmatter scalar that cannot forge structure.

    Every value below is model-authored JSON derived from a document the
    operator did not write. Interpolated raw, a value carrying a newline plus
    ``related_slugs:\\n  - ../../elsewhere`` emits a SECOND ``related_slugs``
    key — and `yaml.safe_load` keeps the last duplicate, so the injected list
    silently replaces the validated one the compiler wrote.

    Newlines are collapsed first (a scalar is one line by construction), then
    anything that is not an obviously-plain scalar is JSON-quoted. Quoting
    rather than stripping matters: a title with a colon is ordinary, and
    mangling it would be a data-loss bug of our own. JSON string syntax is a
    subset of YAML's double-quoted scalar, so this needs no yaml round-trip.
    `VaultManager._wiki_page_path` is the independent second gate.
    """
    collapsed = " ".join(str(value).split())
    if _PLAIN_FM_SCALAR.match(collapsed):
        return collapsed
    return json.dumps(collapsed, ensure_ascii=False)


def _build_wiki_page(page: VaultWikiPage) -> str:
    """Render a VaultWikiPage dataclass into a markdown file.

    Frontmatter shape per `_shared/wiki-page-frontmatter.md` (Phase 2):
    title, type, slug, topic, source_path, date_added, entities, concepts,
    related_slugs, open_questions, backlinks_from. `lint_flags` is absent
    in Phase 2 — Phase 5 writes it.
    """
    fm_lines = [
        "---",
        f"title: {_fm_scalar(page.title)}",
        f"type: {_fm_scalar(page.type)}",
        # Kind tag so the vault graph's color groups fire (Obsidian keys
        # tag:#source); mirrors the memory-store's _inject_kind_tag idiom.
        "tags:",
        f"  - {_fm_scalar(page.type.lower())}",
        f"slug: {_fm_scalar(page.slug)}",
        f"topic: {_fm_scalar(page.topic)}",
        f"source_path: {_fm_scalar(page.source_path)}",
        f"date_added: {_fm_scalar(page.date_added)}",
    ]
    if page.source_hash:
        fm_lines.append(f"source_hash: {_fm_scalar(page.source_hash)}")
    for key, values in (
        ("entities", page.entities),
        ("concepts", page.concepts),
        ("related_slugs", page.related_slugs),
        ("open_questions", page.open_questions),
        ("backlinks_from", page.backlinks_from),
    ):
        if values:
            fm_lines.append(f"{key}:")
            fm_lines.extend(f"  - {_fm_scalar(v)}" for v in values)
        elif key != "entities" and key != "concepts":
            # The three link/question fields are declared even when empty —
            # readers anchor on them; entities/concepts are simply omitted.
            fm_lines.append(f"{key}: []")
    fm_lines.append("---")

    parts = [
        "\n".join(fm_lines),
        f"# {page.title}",
        page.summary,
    ]

    if page.concepts:
        parts.append("## Key Concepts\n" + "\n".join(f"- {c}" for c in page.concepts))

    if page.entities:
        parts.append("## Entities\n" + "\n".join(f"- {e}" for e in page.entities))

    if page.open_questions:
        parts.append("## Open Questions\n" + "\n".join(f"- {q}" for q in page.open_questions))

    if page.related_slugs:
        related = " · ".join(f"[[{s}]]" for s in page.related_slugs)
        parts.append(f"## Related\n{related}")

    parts.append(f"---\n*Source: `{page.source_path}` · Added {page.date_added}*")

    return "\n\n".join(parts) + "\n"


class VaultLibrarian:
    def __init__(
        self,
        vault_manager: VaultManager,
        adapter: ModelAdapter | None,
        adapter_options: AdapterOptions,
        config: "VaultConfig",
        log_dir: Path | None = None,
        agents_dir: Path | None = None,
    ) -> None:
        self._manager = vault_manager
        self._adapter = adapter
        self._adapter_options = adapter_options
        self._config = config
        self._breaker = CircuitBreaker(
            name="vault_librarian",
            max_failures=3,
            log_dir=log_dir,
        )
        self._agents_dir = agents_dir
        self._agent: AgentDefinition | None = None
        # Serializes hub `backlinks_from` read-modify-write against concurrent
        # `compile_source` calls. Without this, two ingests targeting the same
        # hub race and the second atomic swap clobbers the first's append.
        self._backlinks_lock = asyncio.Lock()
        # Serializes whole compiles: slug reservation is check-then-write
        # against the filesystem, so two concurrent same-stem compiles could
        # both claim the base slug and the later write would clobber the
        # first. Compiles are background work; serial is correct and cheap.
        self._compile_lock = asyncio.Lock()

    def _get_agent(self) -> AgentDefinition:
        if self._agent is None:
            self._agent = load_agent("vault-librarian", agents_dir=self._agents_dir)
        return self._agent

    def _get_adapter(self) -> tuple[ModelAdapter | None, AdapterOptions | None]:
        """Return (adapter, options) for the librarian, or (None, None) when unconfigured."""
        if self._adapter is None:
            return None, None
        agent = self._get_agent()
        options = self._adapter_options
        if agent.max_tokens_override is not None:
            options = dataclasses.replace(options, max_output_tokens=agent.max_tokens_override)
        return self._adapter, options

    async def compile_source(self, raw_rel_path: str) -> VaultWikiPage | None:
        """Main pipeline: extract → LLM classify → write wiki page + index + log.

        Serialized end-to-end by `_compile_lock` — slug reservation is a
        filesystem check-then-write, so concurrent compiles must not
        interleave between the check and the page write.
        """
        if self._breaker.is_tripped:
            logger.warning("VaultLibrarian circuit breaker tripped — skipping %s", raw_rel_path)
            return None

        raw_root = (self._manager.root / "raw").resolve()
        vault_abs = (self._manager.root / raw_rel_path).resolve()
        try:
            vault_abs.relative_to(raw_root)
        except ValueError:
            raise VaultContainmentError(
                f"raw_rel_path {raw_rel_path!r} resolves to {vault_abs!r} which is outside vault/raw/"
            )
        if not vault_abs.exists():
            logger.warning("VaultLibrarian: raw file not found: %s", raw_rel_path)
            return None

        async with self._compile_lock:
            return await self._compile_source_locked(raw_rel_path, vault_abs)

    async def _compile_source_locked(
        self, raw_rel_path: str, vault_abs: Path
    ) -> VaultWikiPage | None:
        # Derive slug and title from filename
        slug = _slugify(vault_abs.stem)
        title = vault_abs.stem.replace("-", " ").replace("_", " ").title()
        source_hash = hashlib.sha256(vault_abs.read_bytes()).hexdigest()

        # Skip only when the existing page belongs to THIS source at THIS
        # content hash. Same source, changed bytes → recompile in place
        # (existing backlinks_from survives). Different source_path is a
        # filename collision — disambiguate rather than silently dropping
        # the new source from the wiki.
        resolution = self._resolve_slug_collision(slug, raw_rel_path, source_hash)
        if resolution is None:
            return None
        slug, preserved_backlinks = resolution

        # Extract text
        text = VaultIndexer.extract_text(vault_abs) or ""
        excerpt = text[:self._config.max_extract_chars]

        if not excerpt.strip():
            # Empty-source fallback (Phase 2 contract): write a page with the
            # general topic + sentinel summary, skip the LLM call entirely.
            logger.info("VaultLibrarian: no extractable text from %s — writing empty-source page", raw_rel_path)
            metadata: dict = {"topic": _EMPTY_SOURCE_TOPIC, "summary": _EMPTY_SOURCE_SUMMARY}
            existing_topics: list[str] = []
        else:
            existing_topics = self._extract_existing_topics()
            agent = self._get_agent()
            prompt_template = agent.get_section("Ingest Prompt")
            if not prompt_template:
                logger.warning("VaultLibrarian: vault-librarian.md missing 'Ingest Prompt' section")
                return None

            prompt = prompt_template.format(
                title=title,
                existing_topics=", ".join(existing_topics) if existing_topics else "none yet",
                extracted_text=excerpt,
            )

            adapter, options = self._get_adapter()
            if adapter is None:
                logger.error("VaultLibrarian: no model adapter available")
                return None

            try:
                raw_response = await adapter.generate(prompt, options)
                self._breaker.record_success()
            except Exception as exc:
                self._breaker.record_failure(str(exc))
                logger.error("VaultLibrarian: model call failed for %s: %s", raw_rel_path, exc)
                return None

            metadata = _parse_llm_json(raw_response)
            if not metadata:
                logger.warning("VaultLibrarian: empty/invalid JSON from model for %s", raw_rel_path)
                metadata = {}

        # Defensive filter: drop any related_slugs the LLM invented that do
        # not correspond to a real wiki page. Validated against page slugs
        # (`vault/wiki/{slug}.md`), NOT topic headers — those are disjoint
        # namespaces. Prior to this fix, legitimate graph edges were erased.
        proposed_related = [str(s) for s in metadata.get("related_slugs", [])[:5]]
        related_slugs = [s for s in proposed_related if self._manager.wiki_page_exists(s)]

        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d")

        page = VaultWikiPage(
            slug=slug,
            title=title,
            topic=_slugify(metadata.get("topic", "general")),
            summary=metadata.get("summary", "").strip() or f"Source: {raw_rel_path}",
            entities=[str(e) for e in metadata.get("entities", [])[:10]],
            concepts=[str(c) for c in metadata.get("concepts", [])[:10]],
            open_questions=[str(q) for q in metadata.get("open_questions", [])[:5]],
            related_slugs=related_slugs,
            source_path=raw_rel_path,
            date_added=date_str,
            backlinks_from=preserved_backlinks,
            source_hash=source_hash,
        )

        # Write wiki page
        wiki_content = _build_wiki_page(page)
        self._manager.write_wiki_page(slug, wiki_content)

        # Update INDEX.md
        self._manager.update_wiki_index(
            topic=page.topic,
            slug=slug,
            title=title,
            summary=page.summary[:120],
        )

        # Append to ingest log
        log_entry = (
            f"- **{date_str}** — [{title}]({slug}.md)"
            f" · topic: `{page.topic}`"
            f" · source: `{raw_rel_path}`"
        )
        self._manager.append_ingest_log(log_entry)

        # Phase 3: compound cross-refs — append backlinks to any matching hub pages.
        target_set: set[str] = set(page.related_slugs)
        for name in page.entities + page.concepts:
            hub = _slugify(name)
            if hub:
                target_set.add(hub)
        target_set.discard(slug)

        updated, skipped = await self._append_backlinks(slug, sorted(target_set))
        logger.info(
            "compiled %s → wiki/%s.md (topic=%s) · backlinks updated: %s · skipped (missing): %s",
            raw_rel_path, slug, page.topic, updated, skipped,
        )
        return page

    def _resolve_slug_collision(
        self, slug: str, raw_rel_path: str, source_hash: str
    ) -> tuple[str, list[str]] | None:
        """Return `(slug, preserved_backlinks)` for this source, or None when
        it is already compiled at this content hash.

        Same source_path + same hash → skip (idempotent). Same source_path,
        different hash → recompile under the same slug, carrying the page's
        existing backlinks_from forward. Different source_path → filename
        collision: append the raw folder name, then a numeric suffix."""
        def _own_page(candidate: str) -> tuple[str, list[str]] | None:
            fm = self._manager.read_wiki_page_frontmatter(candidate)
            if str(fm.get("source_path", "")) != raw_rel_path:
                return None
            if str(fm.get("source_hash", "")) == source_hash:
                logger.info(
                    "VaultLibrarian: wiki page for %s is current — skipping", candidate
                )
                return ("", [])  # sentinel: ours and unchanged
            backlinks = [str(b) for b in (fm.get("backlinks_from") or [])]
            logger.info(
                "VaultLibrarian: source %s changed — recompiling %s", raw_rel_path, candidate
            )
            return (candidate, backlinks)

        if not self._manager.wiki_page_exists(slug):
            return (slug, [])
        owned = _own_page(slug)
        if owned is not None:
            return None if owned[0] == "" else owned
        parent = _slugify(Path(raw_rel_path).parent.name)
        candidate = _slugify(f"{slug}-{parent}") if parent else slug
        n = 2
        while self._manager.wiki_page_exists(candidate):
            owned = _own_page(candidate)
            if owned is not None:
                return None if owned[0] == "" else owned
            candidate = _slugify(f"{slug}-{parent}-{n}")
            n += 1
        logger.info(
            "VaultLibrarian: slug collision for %s — compiling %s as %s",
            slug, raw_rel_path, candidate,
        )
        return (candidate, [])

    async def _append_backlinks(
        self, source_slug: str, target_slugs: list[str]
    ) -> tuple[list[str], list[str]]:
        """Append source_slug to each existing target hub's backlinks_from field.

        Returns (updated, missing). Write failures are logged and recorded as
        breaker failures but not re-raised — the source page has already landed
        and lint/heartbeat will close any gaps.

        Holds `self._backlinks_lock` around the read-modify-write so concurrent
        `compile_source` calls targeting the same hub do not race.
        """
        updated: list[str] = []
        missing: list[str] = []
        async with self._backlinks_lock:
            for target in target_slugs:
                if not target or target == source_slug:
                    continue
                if not self._manager.wiki_page_exists(target):
                    missing.append(target)
                    continue
                fm = self._manager.read_wiki_page_frontmatter(target)
                existing = [str(b) for b in (fm.get("backlinks_from") or [])]
                if source_slug in existing:
                    continue  # idempotent — hub already links back
                try:
                    ok = self._manager.update_wiki_backlinks(target, [*existing, source_slug])
                except Exception as exc:
                    self._breaker.record_failure(f"backlink {target}: {exc}")
                    logger.warning("VaultLibrarian: backlink append failed for %s: %s", target, exc)
                    continue
                if ok:
                    updated.append(target)
                else:
                    missing.append(target)
        return updated, missing

    async def backfill_hub_backlinks(self) -> dict[str, list[str]]:
        """Re-link Source pages to hub pages created after they were compiled.

        `compile_source` can only link to hubs that already exist, so a hub
        the operator curates later never hears about earlier sources. For
        every Source page, slugify its entities/concepts and, for each hub
        that exists now: append the source to the hub's ``backlinks_from``
        and the hub to the source's ``related_slugs`` (both frontmatter-only
        merges — bodies are never touched). Idempotent; returns
        ``{source_slug: [newly linked hubs]}`` for sources that changed.
        """
        async with self._compile_lock:
            return await self._backfill_locked()

    async def _backfill_locked(self) -> dict[str, list[str]]:
        # Holding _compile_lock keeps a backfilled backlink from being
        # overwritten by a concurrent recompile of the same source page
        # (compile reads preserved backlinks and writes later under this
        # same lock).
        results: dict[str, list[str]] = {}
        for slug in self._manager.list_wiki_slugs():
            fm = self._manager.read_wiki_page_frontmatter(slug)
            if str(fm.get("type", "")).lower() != "source":
                continue
            names = list(fm.get("entities") or []) + list(fm.get("concepts") or [])
            hubs = {h for h in (_slugify(str(n)) for n in names) if h and h != slug}
            already = set(str(s) for s in (fm.get("related_slugs") or []))
            targets = sorted(
                h for h in hubs - already if self._manager.wiki_page_exists(h)
            )
            if not targets:
                continue
            updated, _missing = await self._append_backlinks(slug, targets)
            linked: list[str] = []
            for hub in targets:
                try:
                    if self._manager.update_wiki_related_slugs(slug, [hub]):
                        linked.append(hub)
                except Exception as exc:
                    logger.warning(
                        "backfill: related_slugs update failed for %s → %s: %s",
                        slug, hub, exc,
                    )
            if linked or updated:
                results[slug] = sorted(set(linked) | set(updated))
        return results

    async def synthesize_query(self, query: str, candidate_slugs: list[str]) -> str:
        """Synthesize an answer from vault wiki pages for a query.

        Reads the whole scoped candidate set under the configured budgets, not
        a fixed prefix of it. The prefix mattered: `_scope_candidates` returns
        seeds first and expanded pages after them, so a fixed head slice cut
        off exactly the compound-wiki traversal that pass 2 exists to perform.
        """
        if not candidate_slugs:
            return "No relevant vault pages found for this query."

        budget = self._config.synthesis_char_budget
        page_chars = self._config.synthesis_page_chars
        wiki_parts: list[str] = []
        used = 0
        for slug in candidate_slugs[: self._config.synthesis_max_pages]:
            if used >= budget:
                break
            content = self._manager.read_wiki_page(slug)
            if not content:
                continue
            # The header and the join separator are part of what the model is
            # sent, so they spend the budget too — counting only the excerpt
            # let the assembled prompt run over the configured cap by roughly
            # one header per page.
            framing = len(f"### {slug}\n") + (2 if wiki_parts else 0)
            room = budget - used - framing
            if room <= 0:
                break
            excerpt = content[: min(page_chars, room)]
            wiki_parts.append(f"### {slug}\n{excerpt}")
            used += len(excerpt) + framing

        if not wiki_parts:
            return "No readable wiki pages found."

        # Adapter first: with no adapter the answer is the concatenated pages
        # either way, and `_get_adapter` short-circuits without loading the
        # agent card that only the prompt path needs.
        adapter, options = self._get_adapter()
        if adapter is None:
            return "\n\n".join(wiki_parts)

        prompt_template = self._get_agent().get_section("Query Prompt")
        if not prompt_template:
            # Fallback: just concatenate summaries
            return "\n\n".join(wiki_parts)

        prompt = prompt_template.format(
            query=query,
            wiki_content="\n\n".join(wiki_parts),
        )

        try:
            result = await adapter.generate(prompt, options)
            self._breaker.record_success()
            return result.strip()
        except Exception as exc:
            self._breaker.record_failure(str(exc))
            logger.warning("VaultLibrarian: query synthesis failed: %s", exc)
            return "\n\n".join(wiki_parts)

    def _extract_existing_topics(self) -> list[str]:
        """Parse existing topic slugs from INDEX.md section headers."""
        index_content = self._manager.read_wiki_index()
        topics: list[str] = []
        for line in index_content.splitlines():
            if line.startswith("## "):
                topic_title = line[3:].strip()
                topics.append(_slugify(topic_title))
        return topics
