"""vault_lint tool — run the five lint passes and return a structured report.

Thin wrapper over `tesseract/memory/vault_lint.py`. Lint is proposal, not action;
`dry_run=True` returns the report without writing any frontmatter changes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

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
    def description(self) -> str:
        return (
            "Run lint passes over the compiled vault wiki. Detects orphan pages, "
            "stale sources, 4-verb contradictions between Source pages, missing "
            "entity hubs, and an INDEX scale-split alarm. Writes `lint_flags:` into "
            "page frontmatter; operator resolves. Pass dry_run=true to inspect "
            "without writing."
        )

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
