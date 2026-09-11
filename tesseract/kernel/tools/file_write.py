"""FileWriteTool — writes content to a file.

Not concurrent-safe, not read-only. Creates parent directories as needed.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult
from tesseract.kernel.tools.receipt import Receipt
from tesseract.paths import readable_state_prefix

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

# The config files the assistant must never write, whatever `permissions.yaml` says.
# `providers.yaml` and `roles.yaml` left this set when delegates were given
# ASK-level edit rights over them.
#
# `mcp.yaml` joined it when the MCP verb floors moved out of source: it became
# the sole authority for what an MCP client may do, and a `path_overrides`
# entry alone would not have held — those apply to `file_write` only, so
# `file_copy`/`file_move` could have overwritten it at their own ASK posture.
# The lock has to live here, where all three write paths share it.
#
# `identity.yaml` joined it when the identity block left `mirror.yaml`: the
# name, the gender and the wake phrase were unreachable while they lived in a
# locked file, and relocating a decision must not quietly change it. The
# operator writes them through Settings -> Identity or by hand.
_LOCKED_CONFIG_FILES: frozenset[str] = frozenset({
    "config/permissions.yaml",
    "config/mirror.yaml",
    "config/mcp.yaml",
    "config/identity.yaml",
})

_LOCKED_CONFIG_NAMES: frozenset[str] = frozenset(
    p.rsplit("/", 1)[-1] for p in _LOCKED_CONFIG_FILES
)

# The records the runtime learns from, and the assistant's own hands may not
# touch, whatever `permissions.yaml` says. Every one is written in-process by
# the runtime (the ledger, the usage logs, the agenda store, the event store,
# the project registry), never through a tool, so nothing legitimate is lost.
# What is lost is the one move a self-improving agent has been measured
# making: scoring well by editing the record that scores it. `runtime/` is
# here by name because, relative to the home tree, it is a decoy path a write
# would create rather than the sibling tree the write boundary already seals.
_RECORD_LOCK_PREFIXES: tuple[str, ...] = (
    "runtime",
    "agenda",
    "logs/usage",
    "logs/skills",
    "logs/workspace",
)
_RECORD_LOCK_FILES: frozenset[str] = frozenset({
    "logs/cost-tracking.jsonl",
    "projects/registry.json",
})


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
        if rel_posix in _RECORD_LOCK_FILES:
            return f"record locked: {rel_posix}"
        for prefix in _RECORD_LOCK_PREFIXES:
            if rel_posix == prefix or rel_posix.startswith(prefix + "/"):
                return f"record locked: {rel_posix}"
    return _locked_config_home_hit(resolved)


class FileWriteInput(BaseModel):
    file_path: str = Field(description="Absolute or workspace-relative path to the file to write")
    content: str = Field(description="The content to write to the file")


class FileWriteTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "propose"

    group: ClassVar[str] = "files-on-disk"
    summary: ClassVar[str] = "Write content to a file, creating parent directories as needed."
    use_when: ClassVar[str] = (
        "Creating a new file or overwriting one wholesale. A relative path "
        "anchors at the state root; writes into locked runtime/source trees are refused."
    )
    not_when: ClassVar[str] = (
        "Use `file_copy` or `file_move` to relocate an existing file instead of "
        "reading and rewriting it."
    )
    depends_on: ClassVar[str] = ""
    # A file's own content is its identifier. A path alone proves nothing: the
    # file at that path a minute later may be somebody else's, and the whole
    # point of the mark is that a later pass can tell.
    receipt_kind: ClassVar[str] = "file"
    recovery_behaviour: ClassVar[str] = "idempotent"

    @property
    def name(self) -> str:
        return "file_write"

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
            if reason.startswith("record locked"):
                msg = (
                    f"{reason}. The runtime writes this record itself and reads "
                    "it to decide what it learned and what it spent, so nothing "
                    "the assistant does may edit it. Read it with file_read."
                )
            else:
                msg = (
                    f"{reason} — the assistant cannot grant itself permissions or "
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
        # `tesseract/`-prefixing normalizer and the state-dir redirect exist
        # to reconcile a code-tree anchor with home-anchored state; with one
        # root for policy, validation and the write itself, there is nothing
        # left to reconcile.
        path = resolved

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(inp.content, encoding="utf-8")
        except OSError as e:
            return ToolResult(output=f"Error writing file: {e}", is_error=True)

        # Fire-and-forget workshop indexing. When the assistant writes any
        # markdown / text under `workshop/`, the resulting artifact becomes
        # recallable via `recall_history` within seconds. Best-effort: failure
        # does not surface to the caller (the write already succeeded).
        # Resolved against the path-validator output so the indexer never sees
        # `..`-traversed targets.
        _maybe_index_workshop_write(path, state_root)

        note = await _maybe_register_tool_write(path, state_root, context)

        return ToolResult(
            output=f"Written {len(inp.content)} bytes to {path}{note}",
            receipt=Receipt(
                kind="file",
                id="sha256:" + sha256(inp.content.encode("utf-8")).hexdigest(),
                locator=str(path),
            ),
        )


async def _maybe_register_tool_write(
    path: Path, state_root: Path, context: ToolContext
) -> str:
    """Register a tool file the moment it is written, and say what happened.

    Writing the file IS the act that should make the tool callable, so the
    scan belongs here. It used to belong to `tool_search`, whose `run()` calls
    `sync_home_tools` before searching — and that was the only mid-conversation
    trigger in the runtime. A provider that discovers deferred tools
    server-side never receives our `tool_search` at all
    (`adapters/base.py::project_tools` drops it), so on that path the scan
    stopped running, a tool written mid-conversation could not become callable
    until the next restart, and the "did not load" error never reached anyone.
    Both callers are kept: this one fires on every provider, `tool_search`
    still fires for the providers that carry it, and `sync_home_tools` is
    idempotent, so a second scan on the same unchanged directory costs one
    `scandir` and one `stat` per file.

    Returns a note to append to the write's own result, which is the only
    place the model is looking, and "" when the write was not a tool file.

    **Every property this note has to hold at once**, because getting one of
    them alone is how the first version of it was wrong:

    - It reports on THIS file and no other. `sync_home_tools` scans the whole
      directory and `LoadReport` answers for the whole scan, so a second file
      written in the same turn, or one already sitting there broken, would
      otherwise have its failure read as this write's.
    - A file can load some of what it defines and fail on the rest.
      `_load_one` returns loaded and failed separately for exactly that case,
      so both halves are said. Reporting only the failure told the model
      nothing had registered when something had.
    - Silence is never the answer for a file that defines no tool. That
      silence is what let a plain module sit in `tools/` looking like a tool.
    - A failure to scan is not a failed write. The write already succeeded,
      and an error here would make the model write the file again.
    - Off the loop: loading imports the file, and an import runs operator code
      of unknown duration.
    """
    if path.suffix.lower() != ".py":
        return ""
    try:
        relative = path.relative_to(state_root).as_posix()
    except (ValueError, OSError):
        return ""
    if readable_state_prefix(relative) != "tools":
        return ""
    provider = context.tool_registry_provider
    if provider is None:
        return ""
    try:
        import asyncio

        from tesseract.kernel.home_tools import sync_home_tools, tools_from_file

        registry = provider()
        if registry is None:
            return ""
        report = await asyncio.to_thread(sync_home_tools, registry)
        mine = tools_from_file(path)
    except Exception:
        return ""

    # `errors` and `refused` are written `"<filename>: <problem>"`, so the
    # prefix is what scopes them to this write. `loaded` carries tool names
    # and cannot be filtered that way, which is what `tools_from_file` is for.
    stem = f"{path.name}: "
    failures = [m[len(stem):] for m in report.errors if m.startswith(stem)]
    refused = [m[len(stem):] for m in report.refused if m.startswith(stem)]

    parts: list[str] = []
    if mine:
        parts.append(f"Registered and callable now: {', '.join(sorted(mine))}.")
    if failures:
        # The contract errors say precisely what is missing and what to write
        # instead, so they are passed through rather than summarised. A generic
        # "must subclass Tool" suffix appended here contradicted them whenever
        # the real problem was a later check.
        parts.append(
            ("The rest of this file did not load" if mine else
             "This file did not load, so nothing it defines is callable")
            + ": " + "; ".join(failures) + "."
        )
    if refused:
        parts.append(f"Refused: {', '.join(refused)}.")
    if not parts:
        return ""
    return ". " + " ".join(parts)


def _maybe_index_workshop_write(path: Path, state_root: Path) -> None:
    """Index ``path`` into the work-history index if it lives under
    ``workshop/`` and looks like a text artifact.

    "Under ``workshop/``" is decided against ``state_root``, not by looking
    for the word anywhere in the absolute path — the install root is
    operator-chosen, so a substring test indexes every markdown file written
    by anyone whose home directory happens to contain "workshop".

    Synchronous: one MD/TXT file is microseconds of FTS5 inserts —
    not worth executor scheduling overhead. Matches the parallel
    `chat_content.index_conversation_file` hook. Silent on any failure:
    the write already succeeded; indexing is a downstream convenience
    and must never surface as a tool error.
    """
    if path.suffix.lower() not in (".md", ".txt"):
        return
    try:
        relative = path.relative_to(state_root).as_posix()
    except (ValueError, OSError):
        return
    if readable_state_prefix(relative) != "workshop":
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
        # `index_conversation_file` in
        # `mirror/server/chat_content.py`.
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
