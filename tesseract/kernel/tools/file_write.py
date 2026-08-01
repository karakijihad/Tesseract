"""FileWriteTool — writes content to a file.

Not concurrent-safe, not read-only. Creates parent directories as needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult

# Source trees, state-root-relative. In a packaged install these are inert —
# source lives in the sealed `app/` tree, which the write boundary denies
# before policy is consulted, and `<home>/kernel/` does not exist. In a DEV
# checkout `home_dir()` IS the source package, so these are the only thing
# enforcing the kernel lockdown there: the write boundary cannot help when the
# state root and the source tree are the same directory.
_RUNTIME_LOCK_PREFIXES: tuple[str, ...] = (
    "kernel",
    "orchestrator",
    "brain",
    "memory",
    "permissions",
    "scheduler",
    "mirror",
    "supervisor",
)

# The two config files TARS must never write, whatever `permissions.yaml`
# says. `providers.yaml` and `roles.yaml` left this set when the trio was
# given ASK-level edit rights over them.
_LOCKED_CONFIG_FILES: frozenset[str] = frozenset({
    "config/permissions.yaml",
    "config/mirror.yaml",
})

_LOCKED_CONFIG_NAMES: frozenset[str] = frozenset(
    p.rsplit("/", 1)[-1] for p in _LOCKED_CONFIG_FILES
)


def _resolve_for_check(path: str, state_root: Path) -> Path:
    """Resolve to absolute path with symlinks + `..` collapsed.

    Relative paths anchor at the state root — the same root `validate_path`
    bounded the write against and the same one `permissions.yaml`'s prefixes
    are written from. Anchoring anywhere else means the layer that decides and
    the layer that writes are talking about different files.
    """
    p = Path(path)
    if not p.is_absolute():
        p = state_root / p
    return p.resolve()


def _locked_config_home_hit(resolved: Path) -> str | None:
    """Deny a locked config filename under the call-time home's config dir.

    Now the only thing standing between a `permissions.yaml` misconfiguration
    and `permissions.yaml` itself, so it matches on the resolved absolute path
    rather than on any caller-supplied form. `home_dir()` re-resolves
    `TESSERACT_HOME` at call time, so a monkeypatched env var (tests) or a
    packaged install is honored without module re-import.
    """
    from tesseract.paths import home_dir

    config_dir = home_dir() / "config"
    try:
        rel = resolved.relative_to(config_dir.resolve())
    except ValueError:
        return None  # not under the call-time config dir
    rel_posix = rel.as_posix().lower()
    if rel_posix in _LOCKED_CONFIG_NAMES:
        return f"runtime config locked: config/{rel_posix}"
    return None


def _check_runtime_lockdown(resolved: Path) -> str | None:
    """Return a deny-reason if `resolved` is locked runtime, else None.

    Called against an already-resolved path — symlink-escape and `..` traversal
    cannot bypass this. Both checks read off the state root, the same anchor
    the policy prefixes and the write boundary use.
    """
    from tesseract.paths import home_dir

    try:
        rel = resolved.relative_to(home_dir().resolve())
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

        from tesseract.paths import home_dir

        state_root = home_dir()
        try:
            resolved = _resolve_for_check(inp.file_path, state_root)
        except (OSError, RuntimeError) as exc:
            return ToolResult(output=f"path resolution failed: {exc}", is_error=True)

        reason = _check_runtime_lockdown(resolved)
        if reason is not None:
            msg = (
                f"{reason} — TARS cannot grant himself permissions or "
                "reconfigure the Mirror server. The operator edits these two "
                "files by hand or in Settings; every other file under config/ "
                "is writable at ASK."
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

        # Write to the path the permission layers actually evaluated. The
        # `tesseract/`-prefixing normalizer and the state-dir redirect that
        # used to sit here both existed to reconcile a code-tree anchor with
        # home-anchored state; with one root for policy, validation and the
        # write itself, there is nothing left to reconcile.
        path = resolved

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
