"""Permission policy loader — resolves a tool call's posture from permissions.yaml.

Policy is the *operator-configurable* layer. The *security* layer (hardcoded
DENY rules in bash_security.py and equivalents) is separate and non-negotiable.

Resolution order for a tool call (first hit wins):
  1. bash_readonly_allowlist / bash_readonly_exact_allowlist — `bash` tool
     only; a read-only command (pytest scoped under tesseract/tests/, git
     status/log/diff/show, the boot-smoke probe) resolves AUTO regardless
     of mode (G-1, lean-agent-os Phase 1 Task 5; exact-match split added
     2026-07-02 review fix). Never reached unless `bash_security.py`'s
     checks already passed the command.
  2. path_overrides[tool] — match the tool-input path against listed prefixes
  3. modes[current_mode].overrides[tool] — mode-specific posture
  4. tools[tool] — operator override from permissions.yaml
  5. tool class `default_posture` — the tool's own declared baseline
     (attached at boot via `attach_class_defaults`); single source of truth
     for "what does this tool default to?" so a missing yaml entry no longer
     silently falls through to ASK
  6. ASK — last-resort fallback
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

import yaml
from pydantic import BaseModel

from tesseract.kernel.tools.base import PermissionResult
from tesseract.permissions.readonly_commands import is_readonly_allowed

logger = logging.getLogger(__name__)

_VALID_POSTURES = {"auto", "ask", "deny"}
_VALID_MODES = {"max", "standard", "headless"}

DEFAULT_POSTURE = "ask"


class PermissionPolicy:
    def __init__(
        self,
        tools_defaults: dict[str, str],
        modes: dict[str, dict[str, Any]],
        path_overrides: dict[str, list[dict[str, Any]]],
        current_mode: str,
        workspace_root: str | None = None,
        bash_readonly_allowlist: list[str] | None = None,
        bash_readonly_exact_allowlist: list[str] | None = None,
    ) -> None:
        self.tools_defaults = tools_defaults
        self.modes = modes
        self.path_overrides = path_overrides
        self._mode = current_mode
        # G-1 — prefix read-only bash commands (pytest scoped under
        # tesseract/tests/, git reads, boot probe path-scoped entries).
        # None/empty means the carve-out is inert; callers that don't pass
        # it (most test fixtures) get pre-G-1 behavior.
        self._bash_readonly_allowlist = list(bash_readonly_allowlist or [])
        # Whole-string-only companion list (2026-07-02 review fix) — the
        # curl health probe and bare-dir pytest invocations live here so
        # no trailing argument, bundled short-flag cluster included, can
        # ride along a match.
        self._bash_readonly_exact_allowlist = list(bash_readonly_exact_allowlist or [])
        # Populated by `attach_class_defaults` after the tool registry is
        # built. Survives `reload()` — yaml edits don't drop class defaults.
        self._class_defaults: dict[str, str] = {}
        # workspace_root is the project root path. When set, `_path_posture`
        # rewrites absolute paths inside the workspace to forward-slash
        # relative form before prefix-matching `path_overrides`. Without
        # this, an absolute file_path argument bypasses the kernel-lockdown
        # DENY rules (which are written as relative prefixes like
        # `tesseract/brain/`). Audit fix: 2026-04-29.
        self._workspace_root = (
            Path(workspace_root).resolve() if workspace_root else None
        )

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        m = mode.strip().lower()
        if m not in _VALID_MODES:
            raise ValueError(f"unknown security_mode {mode!r}; expected one of {sorted(_VALID_MODES)}")
        self._mode = m

    def reload(self, path: Path) -> None:
        """Phase 18 — re-read `permissions.yaml` and replace internal state
        in place. Same validation as `load_permission_policy` so a malformed
        edit raises before anything mutates. The current `mode` is taken
        from the file; operators editing yaml directly are presumed to
        intend the new `security_mode` value. ``_class_defaults`` is NOT
        re-loaded — it comes from registered tool classes and is stable
        across yaml edits.
        """
        fresh = load_permission_policy(
            path,
            workspace_root=str(self._workspace_root) if self._workspace_root else None,
        )
        self.tools_defaults = fresh.tools_defaults
        self.modes = fresh.modes
        self.path_overrides = fresh.path_overrides
        self._mode = fresh._mode
        self._workspace_root = fresh._workspace_root
        self._bash_readonly_allowlist = fresh._bash_readonly_allowlist
        self._bash_readonly_exact_allowlist = fresh._bash_readonly_exact_allowlist

    def attach_class_defaults(self, defaults: Mapping[str, str]) -> None:
        """Wire each registered tool's class-declared baseline posture into
        the resolver. Called once from `boot.build_tool_registry` after every
        tool is registered. Subsequent yaml reloads keep this dict intact.
        """
        self._class_defaults = {
            name: posture for name, posture in defaults.items()
        }

    def merge_class_defaults(self, defaults: Mapping[str, str]) -> None:
        """Additively register class-declared baselines for tools that come up
        AFTER boot (dynamically-registered MCP-client tools). Unlike
        ``attach_class_defaults`` (full replace, called once from
        ``build_tool_registry``), this merges so the static set survives. Lets
        an external tool resolve to its declared floor instead of the
        last-resort ASK fallback, and keeps ``permissions.yaml`` able to
        override it by name.
        """
        self._class_defaults.update(dict(defaults))

    def class_default(self, tool_name: str) -> str | None:
        """The tool class's declared baseline (None if the tool wasn't
        registered with a default — typically only happens in unit tests
        that bypass `build_tool_registry`)."""
        return self._class_defaults.get(tool_name)

    def get_posture(self, tool_name: str, tool_input: BaseModel) -> PermissionResult:
        return _to_result(self.resolve_posture(tool_name, tool_input.model_dump()))

    def resolve_posture(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Resolve effective posture string for a tool call. Walks
        bash-readonly-allowlist → path → mode → yaml override →
        tool class default → ASK fallback."""
        posture = (
            self._bash_readonly_posture(tool_name, tool_input)
            or self._path_posture(tool_name, tool_input)
            or self._mode_posture(tool_name)
            or self.tools_defaults.get(tool_name)
            or self._class_defaults.get(tool_name)
        )
        if posture is None:
            logger.warning("tool %s has no posture entry — defaulting to ASK", tool_name)
            posture = "ask"
        return posture

    def default_posture(self, tool_name: str) -> str:
        """Mode-aware default posture for a tool, ignoring path overrides.
        Used by the Settings/tools view to render baseline behavior. Falls
        through yaml override → class default → ASK so a tool without a
        yaml entry still surfaces the posture its class declared."""
        return (
            self._mode_posture(tool_name)
            or self.tools_defaults.get(tool_name)
            or self._class_defaults.get(tool_name)
            or DEFAULT_POSTURE
        )

    def has_path_overrides(self, tool_name: str) -> bool:
        return bool(self.path_overrides.get(tool_name))

    def has_mode_override(self, tool_name: str) -> bool:
        return self._mode_posture(tool_name) is not None

    def _bash_readonly_posture(self, tool_name: str, tool_input: dict[str, Any]) -> str | None:
        """G-1 — `bash` calls whose `command` matches the configured
        read-only allowlist resolve AUTO regardless of mode. Returns
        None (no opinion) for every other tool/command so resolution
        falls through to path/mode/default as usual."""
        if tool_name != "bash" or not (
            self._bash_readonly_allowlist or self._bash_readonly_exact_allowlist
        ):
            return None
        command = str(tool_input.get("command") or "")
        if not command:
            return None
        if is_readonly_allowed(
            command, self._bash_readonly_allowlist, self._bash_readonly_exact_allowlist
        ):
            return "auto"
        return None

    def _path_posture(self, tool_name: str, tool_input: dict[str, Any]) -> str | None:
        rules = self.path_overrides.get(tool_name) or []
        if not rules:
            return None
        path = str(tool_input.get("file_path") or tool_input.get("path") or "")
        if not path:
            return None
        path_norm = self._normalize_for_prefix_match(path)
        for rule in rules:
            prefix = str(rule.get("path_prefix", ""))
            if prefix and path_norm.startswith(prefix):
                return str(rule.get("posture", "ask")).lower()
        return None

    def _normalize_for_prefix_match(self, raw_path: str) -> str:
        """Normalize a tool input path for prefix-matching against `path_overrides`.

        DENY rules in `permissions.yaml` are written as project-relative
        prefixes (e.g. `tesseract/brain/`). Two bypasses must be closed:

        1. Absolute paths inside the workspace (`/Users/.../tesseract/
           brain/chat.py`) — without normalization the relative prefix
           never matches.
        2. Relative paths with `..` traversal that start under an AUTO
           prefix (`tesseract/memory-store/../brain/chat.py`) — without
           resolution the AUTO prefix matches first and the DENY rule
           never fires.

        Resolve every input — relative or absolute — against
        `workspace_root`, then re-derive the relative form for prefix
        matching. Audit C1 fix (2026-04-29) + reviewer follow-up.
        """
        path_norm = raw_path.replace("\\", "/")
        if self._workspace_root is None:
            return path_norm
        try:
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = self._workspace_root / candidate
            try:
                rel = candidate.resolve().relative_to(self._workspace_root)
                path_norm = str(rel).replace("\\", "/")
            except (ValueError, OSError):
                # path outside workspace after resolution — leave as-is so
                # `validate_path` (workspace boundary check) reacts.
                pass
        except (ValueError, OSError):
            pass
        return path_norm

    def _mode_posture(self, tool_name: str) -> str | None:
        block = self.modes.get(self._mode) or {}
        overrides = block.get("overrides") or {}
        val = overrides.get(tool_name)
        return str(val).lower() if val is not None else None


def _to_result(posture: str) -> PermissionResult:
    p = posture.lower()
    if p == "auto":
        return PermissionResult.PASSTHROUGH
    if p == "ask":
        return PermissionResult.ASK
    if p == "deny":
        return PermissionResult.DENY
    logger.warning("unknown posture %r — defaulting to ASK", posture)
    return PermissionResult.ASK


def load_permission_policy(
    path: Path,
    workspace_root: str | None = None,
) -> PermissionPolicy:
    if not path.exists():
        raise FileNotFoundError(f"permissions config missing: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    mode = str(raw.get("security_mode") or "max").strip().lower()
    if mode not in _VALID_MODES:
        raise ValueError(f"invalid security_mode {mode!r}; expected one of {sorted(_VALID_MODES)}")

    tools = {k: str(v).strip().lower() for k, v in (raw.get("tools") or {}).items()}
    for name, posture in tools.items():
        if posture not in _VALID_POSTURES:
            raise ValueError(f"invalid posture {posture!r} for tool {name!r}")

    modes = raw.get("modes") or {}
    for mode_name in modes:
        if mode_name not in _VALID_MODES:
            raise ValueError(f"unknown mode {mode_name!r} in permissions.yaml")

    path_overrides = raw.get("path_overrides") or {}

    if "bash_readonly_allowlist" not in raw:
        raise ValueError(
            "permissions.yaml missing required key 'bash_readonly_allowlist' — "
            "G-1 read-only self-verification carve-out has no config-defined "
            "allowlist to consult. Add the key (may be an empty list) rather "
            "than relying on a hardcoded default."
        )
    bash_readonly_allowlist = raw["bash_readonly_allowlist"] or []
    if not isinstance(bash_readonly_allowlist, list) or not all(
        isinstance(item, str) for item in bash_readonly_allowlist
    ):
        raise ValueError("permissions.yaml 'bash_readonly_allowlist' must be a list of strings")

    if "bash_readonly_exact_allowlist" not in raw:
        raise ValueError(
            "permissions.yaml missing required key 'bash_readonly_exact_allowlist' — "
            "the whole-string-only companion to 'bash_readonly_allowlist' (curl health "
            "probe, bare-dir pytest invocations) has no config-defined list to consult. "
            "Add the key (may be an empty list) rather than relying on a hardcoded default."
        )
    bash_readonly_exact_allowlist = raw["bash_readonly_exact_allowlist"] or []
    if not isinstance(bash_readonly_exact_allowlist, list) or not all(
        isinstance(item, str) for item in bash_readonly_exact_allowlist
    ):
        raise ValueError(
            "permissions.yaml 'bash_readonly_exact_allowlist' must be a list of strings"
        )

    return PermissionPolicy(
        tools_defaults=tools,
        modes=modes,
        path_overrides=path_overrides,
        current_mode=mode,
        workspace_root=workspace_root,
        bash_readonly_allowlist=bash_readonly_allowlist,
        bash_readonly_exact_allowlist=bash_readonly_exact_allowlist,
    )
