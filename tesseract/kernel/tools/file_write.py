"""FileWriteTool — writes content to a file.

Not concurrent-safe, not read-only. Creates parent directories as needed.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import ClassVar

from pydantic import BaseModel, Field, field_validator

from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult

_RUNTIME_LOCK_PREFIXES: tuple[str, ...] = (
    "tesseract/kernel",
    "tesseract/orchestrator",
    "tesseract/brain",
    "tesseract/scheduler",
    "tesseract/mirror",
    "tesseract/supervisor",
)

_LOCKED_CONFIG_FILES: frozenset[str] = frozenset({
    "tesseract/config/permissions.yaml",
    "tesseract/config/roles.yaml",
    "tesseract/config/providers.yaml",
    "tesseract/config/mirror.yaml",
})

_LOCKED_CONFIG_NAMES: frozenset[str] = frozenset(
    p.rsplit("/", 1)[-1] for p in _LOCKED_CONFIG_FILES
)


def _resolve_for_check(path: str, workspace_root: Path) -> Path:
    """Resolve to absolute path with symlinks + `..` collapsed.

    `FileWriteInput._normalize_under_tesseract` has already prepended `tesseract/`
    for bare-relative paths so the lock-list strings line up.
    """
    p = Path(path)
    if not p.is_absolute():
        p = workspace_root / p
    return p.resolve()


def _locked_config_home_hit(resolved: Path) -> str | None:
    """Absolute fallback: deny a locked config filename under the call-time
    home's config dir, even when `resolved` falls entirely outside
    `workspace_root`.

    `workspace_root` is always the CODE tree (`ROOT`). Once config lives at
    `<home>/config/` (relocated install, home != code tree), a write to
    `<home>/config/permissions.yaml` is outside `workspace_root` and the
    rel-path check in `_check_runtime_lockdown` never fires — this closes
    that hole. `home_dir()` re-resolves `TESSERACT_HOME` at call time so a
    monkeypatched env var (tests) or a packaged install is honored without
    module re-import.
    """
    from tesseract.paths import home_dir

    config_dir = home_dir() / "config"
    try:
        rel = resolved.relative_to(config_dir.resolve())
    except ValueError:
        return None  # not under the call-time config dir
    rel_posix = rel.as_posix().lower()
    if rel_posix in _LOCKED_CONFIG_NAMES:
        return f"runtime config locked: tesseract/config/{rel_posix}"
    return None


def _check_runtime_lockdown(resolved: Path, workspace_root: Path) -> str | None:
    """Return a deny-reason if `resolved` is in the runtime lock, else None.

    Called against an already-resolved path — symlink-escape and `..` traversal
    cannot bypass this.
    """
    try:
        rel = resolved.relative_to(workspace_root.resolve())
    except ValueError:
        rel = None
    if rel is not None:
        rel_posix = rel.as_posix().lower()
        if rel_posix in _LOCKED_CONFIG_FILES:
            return f"runtime config locked: {rel_posix}"
        for prefix in _RUNTIME_LOCK_PREFIXES:
            if rel_posix == prefix or rel_posix.startswith(prefix + "/"):
                return f"runtime-tree path locked: {rel_posix}"
    return _locked_config_home_hit(resolved)


class FileWriteInput(BaseModel):
    file_path: str = Field(description="Absolute or workspace-relative path to the file to write")
    content: str = Field(description="The content to write to the file")

    @field_validator("file_path", mode="after")
    @classmethod
    def _normalize_under_tesseract(cls, value: str) -> str:
        # 2026-05-17: TARS' `workspace_root` is the repo top so the
        # `permissions.yaml::path_overrides` (prefix `tesseract/kernel/`,
        # `tesseract/brain/`, …) match correctly. But a bare relative path
        # like `kernel/foo.py` would resolve to `<repo>/kernel/foo.py` —
        # the lockdown DENY rule never matches and the write slips through.
        # Normalize at validation time so the policy layer + `validate_path`
        # both see the canonical `tesseract/...` form BEFORE deciding.
        if not value:
            return value
        raw = value.replace("\\", "/")
        if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
            return value  # absolute — leave for path_validator / lockdown
        head = PurePosixPath(raw).parts[:1]
        if head and head[0] == "tesseract":
            return raw  # already canonical
        return str(PurePosixPath("tesseract") / raw)


class FileWriteTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "propose"

    @property
    def name(self) -> str:
        return "file_write"

    @property
    def description(self) -> str:
        return "Write content to a file. Creates the file and parent directories if they don't exist."

    @property
    def input_schema(self) -> type[BaseModel]:
        return FileWriteInput

    def is_concurrency_safe(self) -> bool:
        return False

    def is_read_only(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = tool_input if isinstance(tool_input, FileWriteInput) else FileWriteInput(**tool_input.model_dump())

        try:
            resolved = _resolve_for_check(inp.file_path, Path(context.workspace_root))
        except (OSError, RuntimeError) as exc:
            return ToolResult(output=f"path resolution failed: {exc}", is_error=True)

        reason = _check_runtime_lockdown(resolved, Path(context.workspace_root))
        if reason is not None:
            msg = (
                f"{reason} — TARS cannot edit the live runtime. "
                "Delegate new tools to Claude/Codex for operator review and promotion, "
                "or write to workspace/ / agents/ / tars-workshop/."
            )
            try:
                from tesseract.workspace_events.runtime_lock import emit_runtime_lock_deny

                emit_runtime_lock_deny(
                    tool="file_write",
                    locked_path=str(resolved),
                    reason=reason,
                )
            except Exception:  # noqa: BLE001
                pass  # emitter already best-effort; this is double-belt
            return ToolResult(output=msg, is_error=True, denied_hard=True, deny_reason=msg)

        path = Path(inp.file_path)
        if not path.is_absolute():
            # `FileWriteInput._normalize_under_tesseract` has already
            # prepended `tesseract/` if missing, so the policy layer
            # (decide.evaluate + path_overrides) saw the canonical form.
            path = Path(context.workspace_root) / path

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(inp.content, encoding="utf-8")
        except OSError as e:
            return ToolResult(output=f"Error writing file: {e}", is_error=True)

        # CR-1 (2026-05-22) — fire-and-forget workshop indexing. When TARS
        # writes any markdown / text under `tars-workshop/`, the resulting
        # artifact becomes recallable via `recall_history` within seconds.
        # Best-effort: failure does not surface to the caller (the write
        # already succeeded). Resolved against the path-validator output
        # so the indexer never sees `..`-traversed targets.
        _maybe_index_workshop_write(path)

        return ToolResult(output=f"Written {len(inp.content)} bytes to {path}")


def _maybe_index_workshop_write(path: Path) -> None:
    """Index ``path`` into the work-history index if it lives under
    ``tars-workshop/`` and looks like a text artifact.

    Synchronous: one MD/TXT file is microseconds of FTS5 inserts —
    not worth executor scheduling overhead. Matches the parallel
    `session_store.index_conversation_file` hook. Silent on any failure:
    the write already succeeded; indexing is a downstream convenience
    and must never surface as a tool error.
    """
    if path.suffix.lower() not in (".md", ".txt"):
        return
    try:
        if "tars-workshop" not in path.as_posix():
            return
    except Exception:
        return
    try:
        import os

        from tesseract.memory.work_index import WorkIndex
        from tesseract.memory.work_ingester import index_workshop_file
        from tesseract.paths import TESSERACT_HOME as _DEFAULT_HOME

        # Canonical env-or-default home: test fixtures override via
        # `monkeypatch.setenv`; production uses the resolved constant
        # (`tesseract.paths.TESSERACT_HOME` already defaults to
        # `tesseract/` when the env var is unset). Same pattern as
        # `index_conversation_file` in `session_store.py`.
        home = Path(os.environ.get("TESSERACT_HOME") or _DEFAULT_HOME)
        db_path = home / "work_index.sqlite"
        idx = WorkIndex(db_path)
        try:
            index_workshop_file(idx, path)
        finally:
            try:
                idx.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
