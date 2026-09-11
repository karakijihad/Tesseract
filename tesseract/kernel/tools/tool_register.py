"""``tool_register`` — the explicit trigger for the tools-folder scan.

`file_write` already registers a tool the moment its file is written
(`file_write.py::_maybe_register_tool_write`), and that is the automatic
path: writing the file IS the act that should make it callable. This is the
second, explicit path the operator asked for, for the moment AFTER the
writing is done — the assistant finished a tool, maybe edited it twice, and
wants to confirm what is actually registered right now without writing
anything.

Both call the same `sync_home_tools`, which is idempotent: a scan of an
unchanged directory costs one `scandir` and one `stat` per file, so running
this after a `file_write` that already synced is cheap, not redundant.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

from pydantic import BaseModel

from tesseract.kernel.home_tools import sync_home_tools
from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult
from tesseract.kernel.tools.receipt import Receipt
from tesseract.paths import user_tools_dir


class ToolRegisterInput(BaseModel):
    """No input. The tools folder is a fixed, per-machine location — there is
    nothing here for a caller to name."""


class ToolRegisterTool(Tool):
    # AUTO: this reads the tools folder and rewrites a record of what is
    # already there. Holding it at ASK would mean a tool that just failed
    # to load cannot even tell the assistant so without a second approval —
    # useless for the one moment this tool exists for.
    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "extending-yourself"
    summary: ClassVar[str] = "Rescan the tools folder and report what registered."
    use_when: ClassVar[str] = (
        "You just finished writing or editing a tool under <home>/tools/ and "
        "want to confirm what is registered right now, without writing "
        "anything else. Also useful after fixing a file that failed to load: "
        "run this again to see whether the fix took."
    )
    not_when: ClassVar[str] = (
        "Writing the file already scans it and reports what happened in the "
        "write's own result. Call this only when you want to check again "
        "without a write, for example after several edits in a row."
    )
    depends_on: ClassVar[str] = ""
    # It rewrites `<home>/tools/INDEX.md`, which is one of the app's own
    # records rather than an external system's, so `record` is the answer
    # and the locator is the file the operator or a later turn can read.
    receipt_kind: ClassVar[str] = "record"
    # Re-running it re-scans the same folder and rewrites the same index; two
    # calls back to back with nothing changed leave the same state as one.
    recovery_behaviour: ClassVar[str] = "idempotent"

    @property
    def name(self) -> str:
        return "tool_register"

    @property
    def input_schema(self) -> type[BaseModel]:
        return ToolRegisterInput

    def is_read_only(self) -> bool:
        return False

    def is_concurrency_safe(self) -> bool:
        return True

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        del tool_input
        if context.tool_registry_provider is None:
            return ToolResult(
                output="tool_register unavailable: registry not wired in this runtime",
                is_error=True,
            )
        registry = context.tool_registry_provider()
        if registry is None:
            return ToolResult(
                output="tool_register unavailable: no live registry", is_error=True
            )

        # Off the loop, exactly as `tool_search` does: this imports every
        # changed file under the tools folder, and an import runs operator
        # code of unknown duration. Health checks, WS heartbeats and other
        # turns keep running while it does.
        report = await asyncio.to_thread(sync_home_tools, registry)
        index_path = user_tools_dir() / "INDEX.md"

        parts: list[str] = []
        if report.loaded:
            parts.append(f"Registered: {', '.join(report.loaded)}.")
        if report.removed:
            parts.append(f"Removed: {', '.join(report.removed)}.")
        if report.refused:
            parts.append(f"Refused: {', '.join(report.refused)}.")
        if report.errors:
            # Verbatim, not summarised: the contract error says precisely
            # what is missing and what to write instead.
            broken = "\n".join(f"- {message}" for message in report.errors)
            parts.append(f"Did not load:\n{broken}")
        if report.unreadable:
            parts.append(report.unreadable)
        if not parts:
            parts.append("No change.")
        parts.append(f"Index written to {index_path}.")

        return ToolResult(
            output="\n".join(parts),
            receipt=Receipt(kind="record", locator=str(index_path)),
            metadata={
                "loaded": list(report.loaded),
                "removed": list(report.removed),
                "refused": list(report.refused),
                "errors": list(report.errors),
                "index_path": str(index_path),
            },
        )


__all__ = ["ToolRegisterTool", "ToolRegisterInput"]
