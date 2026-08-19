"""vault_ingest tool — ingest files into the immutable vault.

Two-phase operation:
  Phase 1 (no confirmed_path): suggest a filing location, return preview.
  Phase 2 (confirmed_path set): copy to vault, index, update catalog.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.memory.vault_indexer import VaultIndexer
from tesseract.memory.vault_manager import VaultManager

if TYPE_CHECKING:
    from tesseract.memory.vault_librarian import VaultLibrarian

logger = logging.getLogger(__name__)


class VaultIngestInput(BaseModel):
    source_path: str = Field(description="Absolute path to the file to ingest")
    title: str = Field(default="", description="Title for the vault entry (defaults to filename)")
    summary: str = Field(default="", description="One-line description of the source")
    tags: list[str] = Field(default_factory=list, description="Tags for metadata")
    source_url: str = Field(default="", description="Original URL if this was downloaded from the web")
    confirmed_path: str = Field(default="", description="Set to the vault-relative path to confirm and execute ingestion")


class VaultIngestTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "propose"

    group: ClassVar[str] = "research-library"
    summary: ClassVar[str] = "Files a local document or URL into the immutable vault and indexes it."
    use_when: ClassVar[str] = (
        "Use to add a new source to the vault. Call once without confirmed_path for a "
        "suggested filing location, then again with confirmed_path to execute."
    )
    not_when: ClassVar[str] = (
        "reading what the vault already holds — that is `vault_query` or `vault_search`."
    )

    def __init__(
        self,
        vault_manager: VaultManager,
        vault_indexer: VaultIndexer | None = None,
        vault_librarian: VaultLibrarian | None = None,
    ) -> None:
        self._manager = vault_manager
        self._indexer = vault_indexer
        self._librarian = vault_librarian

    @property
    def name(self) -> str:
        return "vault_ingest"

    @property
    def input_schema(self) -> type[BaseModel]:
        return VaultIngestInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = tool_input if isinstance(tool_input, VaultIngestInput) else VaultIngestInput(**tool_input.model_dump())

        source = Path(inp.source_path)
        if not source.exists():
            return ToolResult(output=f"Source file not found: {inp.source_path}", is_error=True)
        if not source.is_file():
            return ToolResult(output=f"Source path is not a file: {inp.source_path}", is_error=True)

        title = inp.title or source.stem.replace("-", " ").replace("_", " ").title()
        suggested = self._manager.suggest_filing_path(
            source.name,
            source_url=inp.source_url or None,
        )

        # Phase 1: suggest only — also offer the raw/ path as primary option
        if not inp.confirmed_path:
            raw_path = self._manager.suggest_raw_filing_path(source.name)
            return ToolResult(
                output=(
                    f"Suggested vault location (raw inbox): {raw_path}\n"
                    f"Alternative (categorized): {suggested}\n"
                    f"Title: {title}\n"
                    f"Source type: {source.suffix.lstrip('.')}\n"
                    f"File size: {source.stat().st_size:,} bytes\n\n"
                    f"To proceed, call vault_ingest again with confirmed_path set "
                    f"(use the raw path for automatic wiki compilation, or the categorized path)."
                ),
            )

        # Phase 2: execute ingestion
        vault_rel_path = inp.confirmed_path

        try:
            vault_abs = self._manager.file_to_vault(source, vault_rel_path)
        except ValueError as e:
            return ToolResult(output=str(e), is_error=True)
        except FileExistsError:
            return ToolResult(
                output=f"Vault file already exists at {vault_rel_path}. Choose a different path.",
                is_error=True,
            )
        except OSError as e:
            return ToolResult(output=f"Failed to copy to vault: {e}", is_error=True)

        # Write meta sidecar
        now = datetime.now(timezone.utc)
        meta: dict = {
            "source_type": source.suffix.lstrip("."),
            "ingested_at": now.isoformat(),
            "tags": inp.tags,
            "notes": inp.summary,
        }
        if inp.source_url:
            meta["source_url"] = inp.source_url
            meta["content_hash"] = f"sha256:{hashlib.sha256(source.read_bytes()).hexdigest()}"

        self._manager.write_meta_sidecar(vault_rel_path, meta)

        # Index for search (embedding-dependent — skipped when indexer offline)
        if self._indexer is not None:
            chunks_indexed = await self._indexer.index_vault_file(
                vault_rel_path, title, vault_abs,
            )
            index_line = f"Chunks indexed: {chunks_indexed}"
        else:
            index_line = "Chunks indexed: 0 (indexer offline — embeddings unavailable)"

        # Update catalog
        category = self._manager.category_from_path(vault_rel_path)
        self._manager.update_catalog(
            vault_rel_path=vault_rel_path,
            title=title,
            summary=inp.summary or f"{source.suffix.lstrip('.')} file",
            category=category,
            source_url=inp.source_url or None,
        )

        # Wiki compile — only for raw/ filings with a live librarian.
        # Failure is non-fatal; vault_ingest is eventually-consistent.
        wiki_line = ""
        if vault_rel_path.startswith("raw/") and self._librarian is not None:
            try:
                page = await self._librarian.compile_source(vault_rel_path)
                if page is not None:
                    wiki_line = f"\nWiki page: wiki/{page.slug}.md (topic: {page.topic})"
            except Exception as exc:
                logger.warning("vault_ingest: wiki compile failed for %s: %s", vault_rel_path, exc)

        return ToolResult(
            output=(
                f"Ingested: {vault_rel_path}\n"
                f"Title: {title}\n"
                f"{index_line}\n"
                f"Catalog updated."
                f"{wiki_line}"
            ),
        )
