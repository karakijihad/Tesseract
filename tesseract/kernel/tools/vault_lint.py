"""vault_lint tool — run the five lint passes and return a structured report.

Thin wrapper over `tesseract/memory/vault_lint.py`. Lint is proposal, not action;
`dry_run=True` returns the report without writing any frontmatter changes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.adapters.base import AdapterOptions
from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.memory.vault_lint import VaultLinter
from tesseract.memory.vault_manager import VaultManager

if TYPE_CHECKING:
    from tesseract.brain.boot import VaultConfig
    from tesseract.memory.vault_librarian import VaultLibrarian

logger = logging.getLogger(__name__)


class VaultLintInput(BaseModel):
    dry_run: bool = Field(
        default=False,
        description="When True, return the lint report without writing any frontmatter changes or LINT-REPORT.md entries.",
    )


class VaultLintTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "research-library"
    summary: ClassVar[str] = "Runs lint passes over the compiled vault wiki and flags issues found."
    use_when: ClassVar[str] = (
        "Use to check wiki health after ingesting or compiling sources — writes lint_flags "
        "for the operator to resolve. Pass dry_run=true to inspect without writing."
    )
    not_when: ClassVar[str] = (
        "reading vault content — that is `vault_query` or `vault_search`."
    )

    def __init__(
        self,
        vault_manager: VaultManager,
        vault_config: "VaultConfig",
        vault_librarian: "VaultLibrarian | None" = None,
        log_dir: Path | None = None,
        agents_dir: Path | None = None,
    ) -> None:
        self._manager = vault_manager
        self._config = vault_config
        self._librarian = vault_librarian
        self._log_dir = log_dir
        self._agents_dir = agents_dir

    @property
    def name(self) -> str:
        return "vault_lint"

    @property
    def input_schema(self) -> type[BaseModel]:
        return VaultLintInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = tool_input if isinstance(tool_input, VaultLintInput) else VaultLintInput(**tool_input.model_dump())

        adapter, options = self._resolve_adapter()
        linter = VaultLinter(
            vault_manager=self._manager,
            config=self._config,
            adapter=adapter,
            adapter_options=options,
            log_dir=self._log_dir,
            agents_dir=self._agents_dir,
        )
        report = await linter.run(dry_run=inp.dry_run)

        summary = _format_report(report, dry_run=inp.dry_run)
        payload = _report_to_json(report)

        # Backfill pass — hubs created after their sources were compiled
        # gain backlinks (and the sources gain related_slugs) here, since
        # nothing else revisits old pages when a hub appears.
        if not inp.dry_run and self._librarian is not None:
            try:
                backfilled = await self._librarian.backfill_hub_backlinks()
            except Exception as exc:
                logger.warning("vault_lint: hub backfill failed: %s", exc)
                backfilled = {}
            if backfilled:
                payload["hub_backfill"] = backfilled
                summary += f"\n- Hub backlinks backfilled: {len(backfilled)} source page(s)"

        output = f"{summary}\n\n```json\n{json.dumps(payload, indent=2)}\n```"
        return ToolResult(output=output, metadata={"lint_report": payload})

    def _resolve_adapter(self):
        """Borrow the adapter wiring the VaultLibrarian already carries."""
        if self._librarian is None:
            return None, AdapterOptions()
        adapter, options = self._librarian._get_adapter()  # noqa: SLF001 — shared private hook
        return adapter, options or AdapterOptions()


def _format_report(report, dry_run: bool) -> str:
    lines = ["# Vault lint report"]
    if dry_run:
        lines.append("*dry-run — no changes written.*")
    lines.append("")
    lines.append(f"- Orphans: {len(report.orphans)}")
    lines.append(f"- Stale: {len(report.stale)}")
    lines.append(f"- Contradictions: {len(report.contradictions)}")
    lines.append(f"- Missing hubs: {len(report.missing_hubs)}")
    lines.append(
        f"- Scale: {report.scale_page_count} wiki pages "
        f"({'ALARM' if report.scale_alarm else 'under threshold'})"
    )
    if report.failures:
        lines.append("")
        lines.append("## Failures")
        for f in report.failures:
            lines.append(f"- {f}")
    return "\n".join(lines)


def _report_to_json(report) -> dict:
    return {
        "orphans": list(report.orphans),
        "stale": list(report.stale),
        "contradictions": [asdict(c) for c in report.contradictions],
        "missing_hubs": [asdict(m) for m in report.missing_hubs],
        "scale_alarm": report.scale_alarm,
        "scale_page_count": report.scale_page_count,
        "failures": list(report.failures),
    }
