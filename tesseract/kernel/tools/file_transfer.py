"""FileCopyTool / FileMoveTool — copy or move a single file.

File management previously had no dedicated tool, so TARS routed
copies through bash (`copy /Y`, `shutil` one-liners) — a needless trip
through the shell security layer for a pure file operation (live
incident: session 2026-07-12-1818). These tools share `file_write`'s
posture: bare-relative paths anchor at the state root, destinations pass
the locked-config check, and `decide.evaluate` runs `validate_path` on the
write-side fields (see `_WRITE_PATH_TOOLS`) and on the read-side source
(see `_READ_PATH_TOOLS`).

Copy validates only its destination — the source is a read, same
posture as `file_read`. Move validates both ends: removing the source
is a write.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult
from tesseract.kernel.tools.file_write import (
    _check_runtime_lockdown,
    _maybe_index_workshop_write,
    _resolve_for_check,
)


class FileTransferInput(BaseModel):
    source_path: str = Field(description="File to copy/move (absolute or workspace-relative).")
    dest_path: str = Field(
        description=(
            "Full target file path (absolute or workspace-relative), not a "
            "directory. Parent directories are created as needed."
        )
    )
    overwrite: bool = Field(
        default=False, description="Replace dest_path if it already exists."
    )


class _FileTransferTool(Tool):
    default_posture = "ask"
    risk_class: ClassVar[str] = "propose"

    # Subclasses: which input fields must clear the runtime-tree lockdown.
    _lockdown_fields: ClassVar[tuple[str, ...]] = ()

    @property
    def input_schema(self) -> type[BaseModel]:
        return FileTransferInput

    def is_concurrency_safe(self) -> bool:
        return False

    def is_read_only(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    def _transfer(self, source: Path, dest: Path) -> None:
        raise NotImplementedError

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = tool_input if isinstance(tool_input, FileTransferInput) else FileTransferInput(**tool_input.model_dump())

        from tesseract.paths import home_dir

        state_root = home_dir()

        try:
            source = _resolve_for_check(inp.source_path, state_root)
            dest = _resolve_for_check(inp.dest_path, state_root)
        except (OSError, RuntimeError) as exc:
            return ToolResult(output=f"path resolution failed: {exc}", is_error=True)

        for field in self._lockdown_fields:
            resolved = source if field == "source_path" else dest
            reason = _check_runtime_lockdown(resolved)
            if reason is not None:
                msg = (
                    f"{reason} — TARS cannot grant himself permissions or "
                    "reconfigure the Mirror server. The operator edits these "
                    "two files by hand or in Settings."
                )
                try:
                    from tesseract.workspace_events.runtime_lock import emit_runtime_lock_deny

                    emit_runtime_lock_deny(
                        tool=self.name,
                        locked_path=str(resolved),
                        reason=reason,
                    )
                except Exception:  # noqa: BLE001
                    pass  # emitter is best-effort; the DENY below still stands
                return ToolResult(output=msg, is_error=True, denied_hard=True, deny_reason=msg)

        if not source.exists():
            return ToolResult(output=f"source not found: {source}", is_error=True)
        if source.is_dir():
            return ToolResult(
                output=f"source is a directory: {source} — this tool handles single files; transfer files individually.",
                is_error=True,
            )
        if dest.is_dir():
            return ToolResult(
                output=f"dest_path is an existing directory: {dest} — pass the full target file path.",
                is_error=True,
            )
        if dest.exists() and not inp.overwrite:
            return ToolResult(
                output=f"dest exists: {dest} — pass overwrite=true to replace it.",
                is_error=True,
            )

        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            self._transfer(source, dest)
        except OSError as exc:
            return ToolResult(output=f"{self.name} failed: {exc}", is_error=True)

        _maybe_index_workshop_write(dest)
        return ToolResult(output=f"{self.name}: {source} -> {dest}")


class FileCopyTool(_FileTransferTool):
    _lockdown_fields: ClassVar[tuple[str, ...]] = ("dest_path",)

    @property
    def name(self) -> str:
        return "file_copy"

    @property
    def description(self) -> str:
        return (
            "Copy a single file to a new path. Creates parent directories; "
            "refuses to overwrite unless overwrite=true. Use this instead of "
            "bash copy/cp for file management."
        )

    def _transfer(self, source: Path, dest: Path) -> None:
        shutil.copy2(source, dest)


class FileMoveTool(_FileTransferTool):
    _lockdown_fields: ClassVar[tuple[str, ...]] = ("source_path", "dest_path")

    @property
    def name(self) -> str:
        return "file_move"

    @property
    def description(self) -> str:
        return (
            "Move (rename) a single file to a new path. Creates parent "
            "directories; refuses to overwrite unless overwrite=true. Use this "
            "instead of bash move/mv for file management."
        )

    def _transfer(self, source: Path, dest: Path) -> None:
        shutil.move(str(source), str(dest))
