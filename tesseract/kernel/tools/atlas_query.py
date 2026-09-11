"""atlas_query — ask the atlas how things connect.

The walking, the filtering and the citations live in
`orchestrator/atlas/retrieval.py`. This is the wrapper that puts them in the
registry, and therefore in front of the assistant, its sub-agents and MCP
clients alike. One implementation behind all three is the point: two readers of
the same graph are two answers that can disagree.

**And it is where the question becomes a starting point.** `retrieve` takes a
`seed_fn` and defaults to `literal_seeds`, which matches titles, ids and
aliases — deliberately dependency-free, so the contract holds with no
embeddings, no network and no backend. Its own docstring says a caller with the
memory pipeline or the vault index passes its own. This is that caller: seeds
also come from a keyword search over what records SAY, so a question about the
scheduler finds the records that discuss it rather than only the one titled it.

**Every compartment, not just memory.** It used to be memory records only,
because the vault's FTS ids key raw ingest paths and the atlas keys wiki slugs,
and nothing joined the two namespaces. `locate.node_for` joins them now, and it
does it through the FILE rather than by string surgery: two spellings of one
document meet at the document. What that unblocks is the whole point of the
atlas — a question seeded anywhere can walk to anywhere.

Read-only by construction — it opens `atlas.json` and one FTS database, and
returns text.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import ClassVar

from pydantic import BaseModel, Field, field_validator

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.atlas import locate as atlas_locate
from tesseract.orchestrator.atlas import store as atlas_store
from tesseract.orchestrator.atlas.model import NodeKind, Provenance
from tesseract.orchestrator.atlas.retrieval import (
    Query,
    literal_seeds,
    render,
    retrieve,
)

logger = logging.getLogger(__name__)

# Fallbacks used only when `atlas.yaml` cannot be read. The file is the
# authority and normally answers; these exist so a question still gets a
# narrower answer rather than an exception when the config is unreadable.
_SEEDS_WITHOUT_CONFIG = 10
_SCORE_RATIO_WITHOUT_CONFIG = 0.5

#: How many times the seed budget to ask the index for before resolving. A row
#: whose document the map does not hold still occupies a place in the SQL
#: LIMIT, so asking for exactly the budget means one uncited vault chunk costs
#: a seed. Three is measured against nothing and is deliberately generous: the
#: cost of a wider ask is a few more rows scored in SQLite, and the cost of a
#: narrow one is an answer that is quietly thin.
_WIDEN = 3


class AtlasQueryInput(BaseModel):
    query: str = Field(
        default="",
        description="What to look for. Matched against node titles and aliases.",
    )
    start_at: list[str] = Field(
        default_factory=list,
        description="Node ids to start from when you already know them "
                    "(e.g. 'mem:mem_ab12', 'vault:claude').",
    )
    trust: str = Field(
        default=Provenance.INFERRED_BY_MODEL.value,
        description="Lowest acceptable origin for a connection: "
                    "'operator_asserted' (only what the operator wrote), "
                    "'stated_in_source' (also what a record states), or "
                    "'inferred_by_model' (also what a model concluded).",
    )
    kinds: list[str] = Field(
        default_factory=list,
        description="Return only these kinds: memory, wiki_page, raw_source, entity.",
    )
    since: str | None = Field(
        default=None, description="ISO date/time — ignore connections asserted earlier."
    )
    depth: int = Field(default=1, ge=0, le=4,
                       description="How many hops to walk from the starting points.")
    limit: int = Field(default=20, ge=1, le=100)
    token_budget: int = Field(default=2000, ge=1, le=20000)

    @field_validator("trust")
    @classmethod
    def _known_trust(cls, value: str) -> str:
        allowed = {p.value for p in Provenance}
        if value not in allowed:
            raise ValueError(f"trust must be one of {sorted(allowed)}")
        return value

    @field_validator("kinds")
    @classmethod
    def _known_kinds(cls, value: list[str]) -> list[str]:
        allowed = {k.value for k in NodeKind}
        unknown = [k for k in value if k not in allowed]
        if unknown:
            raise ValueError(f"unknown kind(s) {unknown}; allowed: {sorted(allowed)}")
        return value


class AtlasQueryTool(Tool):
    default_posture: ClassVar[str] = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "remembering"
    summary: ClassVar[str] = "Ask how records connect, with a citation for every hop."
    use_when: ClassVar[str] = (
        "Use when the question is about the relationship rather than the "
        "text: what two records have in common, what a subject gathers, why a "
        "run happened, what a decision came out of. A question seeded in one "
        "compartment walks to any other. Every hop says what was read, where, "
        "and who asserted it, so you can decline any of it."
    )
    not_when: ClassVar[str] = (
        "to find a record by what it SAYS, use `memory_search` or "
        "`vault_search`: this reads only the connections between records. And "
        "the recalled-memories block already carries the strongest few "
        "connections of everything it pushed, so asking this about a record "
        "that is in front of you is a call for something you have."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "none"
    recovery_behaviour: ClassVar[str] = "read_only"

    @property
    def name(self) -> str:
        return "atlas_query"

    @property
    def input_schema(self) -> type[BaseModel]:
        return AtlasQueryInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: AtlasQueryInput = tool_input  # type: ignore[assignment]
        if not inp.query.strip() and not inp.start_at:
            return ToolResult(
                output="atlas_query needs either a query or a starting node id.",
                is_error=True,
            )
        atlas = atlas_store.load()
        if not atlas.nodes:
            return ToolResult(
                output=(
                    "The atlas is empty — it is built by the nightly "
                    "`atlas_build` stage, or on demand with "
                    "`python -m tesseract.scheduler.pipeline --stage atlas_build`."
                ),
            )
        try:
            since = _as_utc(inp.since)
        except ValueError:
            return ToolResult(
                output=f"atlas_query: 'since' is not an ISO timestamp: {inp.since!r}",
                is_error=True,
            )
        result = retrieve(
            atlas,
            Query(
                text=inp.query,
                seeds=tuple(inp.start_at),
                min_provenance=Provenance(inp.trust),
                since=since,
                kinds=tuple(NodeKind(k) for k in inp.kinds),
                max_depth=inp.depth,
                token_budget=inp.token_budget,
                limit=inp.limit,
            ),
            seed_fn=content_seeds,
        )
        return ToolResult(output=render(result))


def content_seeds(atlas, query) -> list[str]:
    """Starting points from what records SAY, with title matches kept first.

    Two sources, in this order:

    1. `literal_seeds` — a title, id or alias match. Strongest signal there is:
       the operator named the thing, so a record wearing that name is what they
       meant. These lead, and `retrieve` de-duplicates in order.
    2. Keyword search over the memory FTS index, mapped onto node ids and held
       to a score floor, and mapped onto node ids by `locate.node_for`,
       which is the one place that knows what an id from any store MEANS.

    **The floor is not decoration.** `FTSIndex._sanitize_query` quotes each
    word and joins them with OR, so a two-word question matches a record
    holding either word. Without a floor, "daily brief" seeds on every record
    that says "daily", and the walk spreads from all of them: the answer looks
    like a set of connected things when the only thing they share is a common
    word. The search already scores each hit and the score was previously
    discarded. The floor is a fraction of the best hit rather than a fixed
    number, because BM25 scores are relative to the corpus they came from.

    Fail-soft on purpose. A missing or corrupt index is a worse answer, not an
    error: the literal seeds still stand and the walk still runs, which is
    exactly the behaviour the dependency-free default was written to guarantee.
    Raising here would make an unbuilt search index take the graph down with
    it.
    """
    seeds = list(literal_seeds(atlas, query))
    text = (query.text or "").strip()
    if not text:
        return seeds
    seen = set(seeds)
    for node_id in _fts_node_ids(atlas, text):
        if node_id not in seen:
            seen.add(node_id)
            seeds.append(node_id)
    return seeds


def _seed_limits() -> tuple[int, float]:
    """`(max_content_seeds, min_score_ratio)` from `atlas.yaml`."""
    try:
        from tesseract.orchestrator.atlas.config import load_atlas_config

        cfg = load_atlas_config().query
        return cfg.max_content_seeds, cfg.min_score_ratio
    except Exception:  # noqa: BLE001 — a narrower answer beats an exception
        logger.info("atlas_query: atlas.yaml unreadable; using fallbacks", exc_info=True)
        return _SEEDS_WITHOUT_CONFIG, _SCORE_RATIO_WITHOUT_CONFIG


def _fts_node_ids(atlas, text: str) -> list[str]:
    """Keyword hits as atlas node ids, best-first. `[]` on any failure.

    `retrieve` drops an id the graph does not hold, so a hit on a record the
    builder has not reached yet costs nothing and needs no check here.
    """
    max_seeds, min_ratio = _seed_limits()
    try:
        from tesseract.memory.fts_index import FTSIndex
        from tesseract.paths import home_dir

        # `home_dir()`, not the import-time `TESSERACT_HOME` constant. A
        # constant frozen at import resolves against whichever home was
        # current when this module first loaded, which on a packaged install
        # is the wrong one and in a test is the operator's real store. This
        # repo has found that same bug in the prompt's pointer block and in
        # the brief's resolvers; it is not going to be found here again.
        db = home_dir() / "memory-store" / "derived" / "fts.db"
        # Checked before constructing, not only because a missing index means
        # no seeds: `FTSIndex.__init__` mkdirs its parent and creates the FTS5
        # table, and a tool that calls itself read-only may not bring a store
        # into being just by being asked a question.
        if not db.exists():
            return []
        index = FTSIndex(db_path=db)
        try:
            # TWO searches, and the shape is the whole point.
            #
            # The table is shared, and the LIMIT lands in SQL before anything
            # is resolved, so one compartment can take every place: thirty
            # vault chunks repeating a phrase outrank one memory that says it
            # once. `require_prefix` used to solve that by refusing to look at
            # anything but memory, which was right while nothing else could
            # resolve and is wrong now — it would throw away the
            # cross-compartment answers this join exists to produce.
            #
            # So the memory half is asked for on its own and cannot be crowded
            # out, and everything is asked for beside it. Memory leads on the
            # merge because it is the operator's own record, which is the same
            # precedence `content_seeds` gives a title match over a body one.
            hits = index.search(text, limit=max_seeds, require_prefix="mem_")
            # Wider than the budget: a row whose document the map does not
            # hold still costs a place in the LIMIT, and this is what stops it
            # costing a seed.
            everything = index.search(text, limit=max_seeds * _WIDEN)
        finally:
            # The constructor opens a connection eagerly, so every call would
            # otherwise leave one behind.
            try:
                index.close()
            except Exception:  # noqa: BLE001
                logger.info("atlas_query: closing the index failed", exc_info=True)
    except Exception:  # noqa: BLE001 — a search index is not a reason to fail
        logger.info("atlas_query: content seeding unavailable", exc_info=True)
        return []
    if not hits and not everything:
        return []
    # Relative to the best hit for THIS question, never an absolute number.
    # BM25 scores mean something only next to each other: the same query scores
    # 2e-06 against a one-record store and 4.8 against a full one, so a fixed
    # floor tuned on a mature library returns nothing at all on a new install
    # — a worse failure than the widening it was meant to stop, and a silent
    # one.
    #
    # One floor over both searches, taken from the best hit anywhere. Two
    # floors would let a compartment whose best match is weak set a low bar
    # for itself and flood the seeds with it.
    floor = max(score for _, score in (*hits, *everything)) * min_ratio
    out: list[str] = []
    seen: set[str] = set()
    for row_id, score in (*hits, *everything):
        if score < floor:
            continue
        node_id = atlas_locate.node_for(atlas, row_id)
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        out.append(node_id)
        if len(out) >= max_seeds:
            break
    return out


def _as_utc(raw: str | None) -> datetime | None:
    """An ISO string as an aware UTC datetime, or `None`.

    `datetime.fromisoformat` returns a NAIVE datetime for a string carrying no
    offset, and every edge is stamped aware-UTC, so `retrieve`'s
    `edge.asserted_at < query.since` raised `TypeError: can't compare
    offset-naive and offset-aware datetimes` and took the whole call with it.
    A caller writing `since=2026-08-01` means that date here, not a crash.
    """
    if not raw:
        return None
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


__all__ = ["AtlasQueryInput", "AtlasQueryTool", "content_seeds"]
