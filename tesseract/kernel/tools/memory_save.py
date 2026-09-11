"""memory_save tool — explicitly save a memory.

The markdown file is canonical; the FAISS embedding is derived. Writing
the file is unconditional; embedding is best-effort and skipped when the
embedding backend (Ollama) is unavailable. Skipped memories get picked
up on the next `/rebuild`.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.kernel.tools.receipt import Receipt
from tesseract.memory import dedupe
from tesseract.memory.capture_policy import explain_block
from tesseract.memory.embeddings import EmbeddingIndex
from tesseract.memory.index import MemoryIndex
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import (
    MemoryFrontmatter,
    MemoryType,
    SourceTier,
    lead_paragraph,
)

logger = logging.getLogger(__name__)

# F1 guard: titles that strongly imply the save is a turn echo, not a durable fact.
# When paired with type=user, these route to events/writes.jsonl with
# reason=type_mismatch rather than creating a noisy user/ mem.
_TYPE_MISMATCH_USER_TITLES = re.compile(
    r"^(request(ed)?|asked|last[_ ](asked|requested|said|turn|action|request|query|question|interaction|read)|you[_ ]asked|operator[_ ]asked|user[_ ](asked|requested|sent|wanted|said)|recent |turn[_ ](summary|recap))",
    re.IGNORECASE,
)


class MemorySaveInput(BaseModel):
    type: str = Field(description="Memory type: user, feedback, project, reference, conscience")
    title: str = Field(description="Short title for the memory")
    content: str = Field(
        description=(
            "The memory content to save. Lead with the ACTION/directive, put "
            "backstory last: retrieval injects only the first ~300 chars of a "
            "recalled memory, so 'do X before Y' must come before 'on <date> we "
            "learned…'. For feedback/lessons write the imperative first. "
            "Refer to yourself in the first person ('I should…'), never by "
            "name and never in the third person. A memory written with your "
            "name in it stops being true the moment that name changes."
        )
    )
    summary: str = Field(
        default="",
        description=(
            "The one-line hook for this memory, written for scanning. It is "
            "what the index and the directives block show, so write it "
            "deliberately: the ACTION first, dense, no preamble. 'Close every "
            "surface you open; check for leftover cards before reporting "
            "done.' Not a restatement of the title, and not the first line of "
            "the content. Leave empty only if the content's opening paragraph "
            "already reads that way, and it will be used instead."
        ),
    )
    importance: int = Field(default=5, ge=1, le=10, description="Importance 1-10")
    tags: list[str] = Field(default_factory=list, description="Tags for retrieval")
    source_path: str = Field(default="", description="Vault-relative path if linked to a vault source")
    source_url: str = Field(default="", description="Original URL if derived from a web source")
    source_type: str = Field(default="", description="Source type: chat, upload, paper, article, data, snapshot, imagination, observation")
    # Belief-state fields (spec.md §1, 2026-04-29). Slug is the canonical
    # exact-match key for decisions; expiry_at is an ISO8601 datetime; both
    # are optional and default to no-op when omitted.
    slug: str = Field(default="", description="Canonical exact-match key for decisions (lowercase, e.g. 'voice_default'). Must be unique.")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Saver confidence 0.0-1.0; default 1.0")
    expiry_at: str = Field(default="", description="Optional ISO8601 datetime when this fact should expire from retrieval (e.g. '2026-05-29T00:00:00Z')")
    entities: list[str] = Field(default_factory=list, description="Named entities for exact-entity retrieval (lowercase preferred)")
    subdir: str = Field(default="", description="Optional sub-bucket within the type folder, e.g. 'people' to save into reference/people/. Forward slashes nest (e.g. 'sprints/2026-q2'). The frontmatter type still applies; subdir is organizational only.")


#: What an unattended session's saves are called on the record. Not `chat`,
#: which means the operator said it, and not the caller's own word: the whole
#: point of the field is that a reader ranking a memory can tell where it came
#: from, and a morning's read of the open web ranking as something the operator
#: told us is the failure the provenance floor exists to prevent.
AUTONOMY_SOURCE = "autonomy"


def _source_type_for(inp: "MemorySaveInput", context: ToolContext) -> str:
    """The provenance stamped on this save.

    An autonomy session's is fixed, and the caller cannot name its way out of
    it. Everywhere else the caller's own word wins as it always has, falling
    back to `chat` for something said and `upload` for something with a file
    behind it.
    """
    if context.session_kind == "autonomy":
        return AUTONOMY_SOURCE
    return inp.source_type or ("chat" if not inp.source_path else "upload")


class MemorySaveTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "remembering"
    summary: ClassVar[str] = "Write a new durable memory to the persistent store."
    use_when: ClassVar[str] = (
        "Use to capture a fact, decision, or preference that will still be "
        "true next session rather than only now. One memory per fact, and TAG "
        "it: a tag is how it is found later, and it is also how the map joins "
        "it to everything else filed under the same subject."
    )
    not_when: ClassVar[str] = (
        "use `memory_update` when revising something already saved; use "
        "`diary_append` for self-reflection about the assistant itself."
    )
    depends_on: ClassVar[str] = ""
    # A memory is a record in the app's own store, and its id is what
    # `memory_get` and `memory_update` are handed later. The path is the
    # locator because the store is files on disk and always has been.
    receipt_kind: ClassVar[str] = "record"
    recovery_behaviour: ClassVar[str] = "queryable"

    def __init__(
        self,
        store: MemoryStore,
        index: MemoryIndex,
        embeddings: EmbeddingIndex | None = None,
        auto_linker=None,
        fts_index=None,
    ) -> None:
        self._store = store
        self._index = index
        self._embeddings = embeddings
        self._auto_linker = auto_linker
        self._fts_index = fts_index

    @property
    def name(self) -> str:
        return "memory_save"

    @property
    def input_schema(self) -> type[BaseModel]:
        return MemorySaveInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = tool_input if isinstance(tool_input, MemorySaveInput) else MemorySaveInput(**tool_input.model_dump())


        try:
            mem_type = MemoryType(inp.type)
        except ValueError:
            return ToolResult(output=f"Invalid memory type: {inp.type}", is_error=True)

        # A `feedback` memory is an operator correction: it governs behaviour
        # from then on, in every conversation, attended or not. What the
        # morning reads is the open web and files an outside tool may have
        # written, so a sentence in one of them must not be able to become a
        # standing rule about how the assistant works. Everything else an
        # unattended session learns is welcome, which is why this refuses one
        # type rather than the tool.
        if mem_type is MemoryType.FEEDBACK and context.session_kind == "autonomy":
            return ToolResult(
                output=(
                    "Nobody is watching this conversation, so it cannot save a "
                    "correction about how you work. Save what you learned as "
                    "`project` or `reference` instead, and if it really is a "
                    "rule, say so in a turn the operator is part of."
                ),
                is_error=True,
                caller_error=True,
                metadata={"refused_unattended": True},
            )

        if inp.slug and not re.match(r"^[a-z0-9][a-z0-9_]*$", inp.slug):
            return ToolResult(
                output=(
                    "Invalid slug: must use lowercase letters/digits/underscore "
                    "and start with a letter or digit."
                ),
                is_error=True,
            )

        # F1 type-inference guard: type=user + request-echo/turn-summary title
        # is almost always a junk save. Route through store.log_event so the
        # type_mismatch blocks share the same JSONL sink as store-level blocks.
        if mem_type == MemoryType.USER and _TYPE_MISMATCH_USER_TITLES.match(inp.title or ""):
            try:
                self._store.log_event("writes.jsonl", {
                    "type": "user",
                    "title": inp.title,
                    "status": "blocked",
                    "reason": "type_mismatch",
                })
            except OSError as e:
                logger.warning("type_mismatch forensic log failed: %s", e)
            return ToolResult(
                output=(
                    f"Memory blocked: type_mismatch. Title '{inp.title}' reads as a turn echo; "
                    "user-type memory is for durable operator facts, not request recaps. "
                    "If this is a genuine preference/identity fact, rewrite the title. "
                    "Otherwise skip the save; zero saves is fine."
                ),
                is_error=True,
            )

        # Slug uniqueness — if the operator/agent supplies a slug, ensure no
        # existing memory already claims it. Cheap O(n) scan; same cost as
        # stage_zero_exact and runs once per save.
        if inp.slug:
            for existing in self._store.list_all():
                if existing.slug == inp.slug:
                    return ToolResult(
                        output=(
                            f"Memory blocked: slug '{inp.slug}' already in use by {existing.id} "
                            f"({existing.title}). Use memory_update to revise it, or pick a different slug."
                        ),
                        is_error=True,
                    )

        expiry_dt: datetime | None = None
        if inp.expiry_at:
            try:
                # Accept the trailing 'Z' shorthand alongside +00:00 offsets.
                expiry_dt = datetime.fromisoformat(inp.expiry_at.replace("Z", "+00:00"))
                if expiry_dt.tzinfo is None:
                    expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                return ToolResult(
                    output=f"Invalid expiry_at: {inp.expiry_at!r}; must be ISO8601 (e.g. '2026-05-29T00:00:00Z').",
                    is_error=True,
                )

        now = datetime.now(timezone.utc)
        fm = MemoryFrontmatter(
            id=MemoryFrontmatter.generate_id(),
            type=mem_type,
            title=inp.title,
            summary=(inp.summary.strip() or lead_paragraph(inp.content)),
            created_at=now,
            updated_at=now,
            importance=inp.importance,
            tags=inp.tags,
            entities=inp.entities,
            source_session=context.session_id,
            source_path=inp.source_path,
            source_url=inp.source_url,
            # An unattended save says so, whatever the caller called it. The
            # provenance floor downstream reads this field, and a morning's
            # read of the web filed as `chat` is indistinguishable from
            # something the operator actually said.
            source_type=_source_type_for(inp, context),
            # Stated rather than left to resolve from `source_type`, which is
            # free text here: an unrecognised value resolves to `derived`, and
            # a derived record with nothing to point at is refused. This is
            # the first-hand save path, so what arrives through it is source
            # material whatever the caller called it. A pass that summarises
            # the store writes through the librarian, which declares what it
            # read.
            source_tier=SourceTier.SOURCE,
            slug=inp.slug,
            confidence=inp.confidence,
            expiry_at=expiry_dt,
        )

        body = inp.content
        if inp.source_path:
            wikilink = f"[[{inp.source_path}]]"
            if wikilink not in body:
                body = f"{body}\n\nSource: {wikilink}"

        subdir_override = self._resolve_subdir_override(mem_type, inp.subdir)
        if isinstance(subdir_override, ToolResult):
            return subdir_override

        # Dedupe check — skip the write and refresh the matched entry's
        # updated_at if the body is above the cosine threshold to an existing
        # memory. Fails open when embeddings are offline (dedupe.check returns
        # True, None), so memory writes keep working during Ollama outages.
        if self._embeddings is not None:
            ok, existing_id = await dedupe.check(body, self._embeddings)
            if not ok and existing_id:
                return await self._refresh_existing(existing_id, now)

        if not self._store.write(fm, body, subdir_override=subdir_override):
            return ToolResult(
                output=explain_block(self._store.last_block_reason), is_error=True
            )

        self._index.add(fm)

        embed_note = ""
        if self._embeddings is not None:
            try:
                await self._embeddings.add(fm.id, body)
            except Exception as e:
                logger.warning("embed on save failed for %s: %s", fm.id, e)
                embed_note = "  (not embedded — will index on next /rebuild)"
        else:
            embed_note = "  (not embedded — Ollama offline; will index on /rebuild)"

        if self._fts_index is not None:
            try:
                self._fts_index.add(fm.id, fm.title, body)
            except Exception:
                pass

        link_note = ""
        if self._auto_linker is not None:
            try:
                link_result = await self._auto_linker.auto_link(fm.id, body)
                # Surface degraded auto-linking — the canonical write already
                # succeeded; we just want operators to know the derived
                # `## Related` block didn't get populated this turn.
                degraded_reasons = {
                    "embeddings_failed",
                    "embeddings_unavailable",
                    "no_results",
                    "persist_failed",
                }
                if link_result.status == "skipped" and link_result.reason in degraded_reasons:
                    try:
                        self._store.log_event("writes.jsonl", {
                            "type": "auto_link_skipped",
                            "memory_id": fm.id,
                            "reason": link_result.reason,
                        })
                    except OSError as e:
                        logger.warning("auto_link_skipped forensic log failed: %s", e)
                    if link_result.reason == "persist_failed":
                        link_note = "  (related-link generation failed to persist — see writes.jsonl)"
                    else:
                        link_note = "  (related-link generation skipped — embeddings degraded)"
            except Exception as e:
                # Defensive: auto_link should return AutoLinkResult, never raise.
                # If it does, log and forensic-log instead of swallowing silently.
                logger.warning("auto_link unexpected failure for %s: %s", fm.id, e)
                try:
                    self._store.log_event("writes.jsonl", {
                        "type": "auto_link_error",
                        "memory_id": fm.id,
                        "error": str(e),
                    })
                except OSError:
                    pass
                link_note = "  (related-link generation errored — see writes.jsonl)"

        # A correction saved mid-conversation is a correction that just
        # happened. It lands on the playbook revision read in this session and
        # on the step the reading turn reached, rather than waiting for the
        # session-close reflection to notice a feedback memory exists.
        if mem_type.value == "feedback" and context.session_id:
            try:
                import asyncio

                from tesseract.brain.skill_usage import attribute_session_corrections

                # Off the loop: it reads the usage log whole and a day of
                # turn records, inside a turn.
                # The memory id travels with it: this record IS the
                # correction, and the refinement job's evidence is a step
                # number without it.
                await asyncio.to_thread(
                    attribute_session_corrections, context.session_id, fm.id
                )
            except Exception:  # noqa: BLE001 — telemetry must never break the save
                logger.warning("memory_save: skill-correction attribution failed", exc_info=True)

        slug_note = f" slug={fm.slug}" if fm.slug else ""
        saved_file = self._store.find_file(fm.id)
        saved_path = str(saved_file) if saved_file else ""
        return ToolResult(
            output=f"Memory saved: {fm.id} ({fm.title}){slug_note}{embed_note}{link_note}",
            receipt=Receipt(kind="record", id=fm.id, locator=saved_path),
            metadata={
                "status": "saved",
                "memory_id": fm.id,
                "path": saved_path,
                "title": fm.title,
                "type": mem_type.value,
            },
        )

    @staticmethod
    def _resolve_subdir_override(mem_type: MemoryType, raw: str) -> str | None | ToolResult:
        """Map an operator-supplied `subdir` to a store-relative path.

        Returns None when no subdir was supplied (store falls back to the
        type root). On bad input returns a ToolResult that the caller can
        return directly.
        """
        cleaned = (raw or "").strip().replace("\\", "/").strip("/")
        if not cleaned:
            return None
        if ".." in cleaned.split("/"):
            return ToolResult(
                output=f"Invalid subdir: {raw!r}; relative segments like '..' are not allowed.",
                is_error=True,
            )
        type_prefix = mem_type.value + "/"
        if cleaned == mem_type.value or cleaned.startswith(type_prefix):
            cleaned = cleaned[len(type_prefix):] if cleaned.startswith(type_prefix) else ""
        if not cleaned:
            return None
        return f"{mem_type.value}/{cleaned}"

    async def _refresh_existing(self, existing_id: str, now: datetime) -> ToolResult:
        """Dedupe hit — update the matched entry's `updated_at` instead of writing a new file."""
        existing = self._store.read(existing_id, log_access=False)
        if existing is None:
            logger.warning("dedupe reported %s but it could not be read; skipping refresh", existing_id)
            return ToolResult(
                output=f"Memory deduped: match {existing_id} not readable (stale index?)",
                receipt=Receipt.nothing(),
            )
        existing_fm, existing_body = existing
        existing_fm.updated_at = now
        existing_file = self._store.find_file(existing_id)
        existing_path = str(existing_file) if existing_file else ""
        if not self._store.write(existing_fm, existing_body):
            return ToolResult(
                output=(
                    f"Memory deduped: near-duplicate of {existing_id} ({existing_fm.title}). "
                    "The refresh could not be written, so updated_at was not "
                    "persisted. The record itself is unchanged and still there."
                ),
                is_error=True,
                metadata={
                    "status": "deduped",
                    "memory_id": existing_id,
                    "path": existing_path,
                    "title": existing_fm.title,
                },
            )
        return ToolResult(
            output=(
                f"Memory deduped: near-duplicate of {existing_id} ({existing_fm.title}); "
                "refreshed updated_at instead of creating a new entry."
            ),
            # A refresh is a write. It moved `updated_at` on a real
            # record, so it points at that record rather than at nothing.
            receipt=Receipt(kind="record", id=existing_id, locator=existing_path),
            metadata={
                "status": "deduped",
                "memory_id": existing_id,
                "path": existing_path,
                "title": existing_fm.title,
            },
        )
