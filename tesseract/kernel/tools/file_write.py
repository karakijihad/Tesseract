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


# Relocatable STATE directories: seeded under `<TESSERACT_HOME>/` by
# `config_seed` (workspace, memory-store, vault, tars-workshop) or created on
# demand by the runtime (downloads, uploads). A write to any of them must
# follow `TESSERACT_HOME`, never land in the code tree.
#
# Every entry must be reachable, i.e. not denied outright by
# `permissions.yaml::path_overrides.file_write` — that file is the authority,
# and a dir it denies would never reach this redirect. `agents/` is denied
# there (cards are managed via `agent_create`), so it is deliberately absent.
#
# `config/` is deliberately absent too. The four locked config files are denied
# at the home anchor by `_locked_config_home_hit`, and the remaining ones stay
# workspace_root-relative so a `..`-traversal that collapses into `config/`
# cannot reach the real production config the running app reads — see
# `test_file_write_tars_workshop_traversal_does_not_escape_to_home`.
_STATE_DIRS: tuple[str, ...] = (
    "tars-workshop",
    "downloads",
    "uploads",
    "workspace",
    "vault",
    "memory-store",
)


def _state_home_target(resolved: Path, workspace_root: Path) -> Path | None:
    """If the fully-resolved (symlinks + `..` already collapsed) `resolved`
    path falls under `workspace_root/tesseract/<state-dir>/` for one of
    `_STATE_DIRS`, return the equivalent path anchored at `home_dir()`
    instead — `None` otherwise.

    `workspace_root` here is always the CODE tree (`REPO_ROOT` =
    `TESSERACT_DIR.parent`), which differs from `TESSERACT_HOME` in a packaged
    install (`<home>/app` vs `<home>`). Without this redirect a write lands in
    the code tree: `downloads/paper.pdf` became
    `<home>/app/tesseract/downloads/paper.pdf`, invisible to the runtime,
    unreachable by the file tools, and destroyed by the clean-re-clone path in
    `provision.rs` that `remove_tree`s `app/` after a failed update.

    Only `tars-workshop` was covered before, so every other state dir hit that
    bug. In a dev checkout `home_dir()` IS the code tree's `tesseract/`, so
    this is a no-op there and only changes packaged-install behaviour.

    MUST be called with `resolved` (the already-`.resolve()`d absolute path
    `_check_runtime_lockdown` was evaluated against), never the raw
    unresolved `file_path` string — a raw string containing `..` (e.g.
    `tesseract/tars-workshop/../config/schedule.yaml`) would `startswith`-match
    a state prefix before the `..` collapses, redirecting an unrelated file
    (any `TESSERACT_HOME`-relative path, not just locked config) to
    `home_dir()` — a path `_check_runtime_lockdown` never evaluated at all,
    since it saw the harmless, already-collapsed
    `workspace_root/tesseract/config/schedule.yaml` instead.
    """
    try:
        workspace_root = workspace_root.resolve()
    except OSError:
        return None
    try:
        rel = resolved.relative_to(workspace_root)
    except ValueError:
        return None  # resolved lives outside workspace_root entirely
    rel_posix = rel.as_posix()

    from tesseract.paths import home_dir

    for state_dir in _STATE_DIRS:
        prefix = f"tesseract/{state_dir}"
        if rel_posix != prefix and not rel_posix.startswith(prefix + "/"):
            continue
        tail = rel_posix[len(prefix):].lstrip("/")
        base = home_dir() / state_dir
        target = (base / tail) if tail else base
        # Belt-and-braces containment re-check (mirrors
        # `_locked_config_home_hit` above): `rel` came from an already-resolved
        # path so it cannot itself contain `..`, but confirm the final target
        # still lives under `base` before handing back a write location.
        try:
            target.resolve().relative_to(base.resolve())
        except (OSError, ValueError):
            return None
        return target
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
            # `_state_home_target` takes `resolved` (not `path`/
            # `inp.file_path`) so any `..` has already been collapsed —
            # see its docstring for why the raw string is unsafe here.
            home_target = _state_home_target(resolved, Path(context.workspace_root))
            path = home_target if home_target is not None else Path(context.workspace_root) / path

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
