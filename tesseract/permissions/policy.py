"""Permission policy loader — resolves a tool call's posture from permissions.yaml.

Policy is the *operator-configurable* layer. The *security* layer (hardcoded
DENY rules in bash_security.py and equivalents) is separate and non-negotiable.

Resolution order for a tool call (first hit wins):
  1. bash_readonly_allowlist / bash_readonly_exact_allowlist — `bash` tool
     only; a read-only command (pytest scoped under tesseract/tests/, git
     status/log/diff/show, the boot-smoke probe) resolves AUTO regardless
     of mode. Never reached unless `bash_security.py`'s checks already
     passed the command.
  2. git_readonly_operations — the `git` tool only; the same reads the bash
     allowlist already auto-allows resolve AUTO there too. Without this the
     one-tool path is the STRICTER one for an identical read, which teaches
     the model to reach for `bash` instead — the drift the tool exists to
     prevent.
  3. path_overrides[tool] — match the tool-input path against listed prefixes
  4. modes[current_mode].overrides[tool] — mode-specific posture
  5. custom[tool] — the operator's answer for a tool the ASSISTANT wrote.
     Its own block rather than a line in `tools:` because the two halves are
     different things: `tools:` is what the app ships and is compared key by
     key against the shipped overlay, while these names exist on one machine
     and the overlay carries the block empty. A name may appear in one or the
     other, never both, and the loader refuses a file that says otherwise.

     **4 and 5 are mutually exclusive, not ranked.** The loader also refuses a
     name that a mode's `overrides` and `custom:` both carry, so on any file
     that loads at most one of the two can match a given tool. Read the
     numbering as layout; there is no precedence here to preserve, and no test
     can pin one.
  6. tools[tool] — operator override from permissions.yaml
  7. tool class `default_posture` — the tool's own declared baseline
     (attached at boot via `attach_class_defaults`); single source of truth
     for "what does this tool default to?" so a missing yaml entry no longer
     silently falls through to ASK
  8. modes[current_mode].baseline — the posture a mode gives every tool it
     does not name. It is written LAST so a mode is stated by exception:
     `free` says `baseline: auto` and lists the handful that still ask,
     instead of restating every tool it wants relaxed. A DENY from steps 5
     to 7 is never reached by it, nor is any tool held out by
     `exempt_from_baseline` — widening a mode must not quietly unlock a tool
     somebody closed, and must never reach a tool the assistant wrote.
  9. ASK — last-resort fallback
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml
from pydantic import BaseModel

from tesseract.kernel.tools.base import PermissionResult
from tesseract.permissions.readonly_commands import is_readonly_allowed

logger = logging.getLogger(__name__)

_VALID_POSTURES = {"auto", "ask", "deny"}
#: The security modes this runtime knows. One list, imported by every caller
#: that has to name them (the `/mode` command, its registry entry) so a third
#: mode cannot exist in one place and not another.
#: The mode that keeps an operator in the loop, and the one that runs without
#: one. Named rather than spelled, because `decide.evaluate` has to tell them
#: apart to decide what may run with nobody watching, and a bare "free" in a
#: second module is how a third mode comes to exist in one place and not
#: another.
ATTENDED_MODE = "max"
UNATTENDED_MODE = "free"
VALID_MODES = {ATTENDED_MODE, UNATTENDED_MODE}

# Which of a tool's inputs are WRITE targets. One definition, read by two
# layers that were each about to grow their own: `decide.evaluate` bounds
# these fields with `validate_path`, and `PermissionPolicy._path_posture`
# matches them against `path_overrides`. It used to live in `decide.py` alone,
# so the policy layer knew only `file_path`/`path` and every override written
# for the transfer verbs matched nothing.
#
# `file_copy` names only its destination: its source is a read, bounded by
# `decide._READ_PATH_TOOLS` instead. `file_move` names both, because removing
# the source is a change to it.
WRITE_PATH_FIELDS: dict[str, tuple[str, ...]] = {
    "file_write": ("file_path", "path"),
    "file_copy": ("dest_path",),
    "file_move": ("source_path", "dest_path"),
}

#: How the postures order when more than one applies to a single call. Lower
#: is stricter, so `min` is the answer.
_STRICTNESS = {"deny": 0, "ask": 1, "auto": 2}
#: A mode may hand out AUTO or ASK to the tools it does not name. DENY is not
#: offered: a mode that denies everything refuses `memory_search` and stops the
#: assistant answering at all, which is a broken install, not a security level.
_VALID_BASELINES = {"auto", "ask"}

DEFAULT_POSTURE = "ask"


class PermissionPolicy:
    def __init__(
        self,
        tools_defaults: dict[str, str],
        modes: dict[str, dict[str, Any]],
        path_overrides: dict[str, list[dict[str, Any]]],
        current_mode: str,
        workspace_root: str | None = None,
        custom_defaults: dict[str, str] | None = None,
        bash_readonly_allowlist: list[str] | None = None,
        bash_readonly_exact_allowlist: list[str] | None = None,
        git_readonly_operations: list[str] | None = None,
        workspace_documents: dict[str, Any] | None = None,
        workspace_cards: dict[str, Any] | None = None,
    ) -> None:
        self.tools_defaults = tools_defaults
        # `custom:` — the operator's posture for tools the ASSISTANT wrote,
        # read before `tools:` because a name can appear in only one of them
        # and this is the one that says which half a tool belongs to.
        #
        # It lives in `permissions.yaml` rather than beside the tools it names
        # because this file is DENY to the `file_write` tool and that one was
        # ASK, so the record of what a self-written tool may do was reachable
        # by the same gate that writes the tools. The old justification for the
        # split, that `permissions.yaml` ships verbatim, stopped being true
        # when `config/_shipping/permissions.yaml` became the overlay.
        #
        # **What this does NOT buy, stated because it is easy to overclaim.**
        # The DENY binds tool CALLS. A custom tool's module runs as ordinary
        # in-process Python, so its own code can open this file and write it
        # with no gate in the way. What stands between the assistant and that
        # is the ASK on `file_write` into `tools/`, which is an operator
        # approving a file rather than a guarantee about its contents. The
        # move raises the floor; it does not seal the room.
        self.custom_defaults = custom_defaults or {}
        self.modes = modes
        self.path_overrides = path_overrides
        self._mode = current_mode
        # Prefix read-only bash commands (pytest scoped under
        # tesseract/tests/, git reads, boot probe path-scoped entries).
        # None/empty means the carve-out is inert, which is what most test
        # fixtures get by not passing it.
        self._bash_readonly_allowlist = list(bash_readonly_allowlist or [])
        # Whole-string-only companion list — the curl health probe and
        # bare-dir pytest invocations live here so
        # no trailing argument, bundled short-flag cluster included, can
        # ride along a match.
        self._bash_readonly_exact_allowlist = list(bash_readonly_exact_allowlist or [])
        # The `git` operations that only read. The bash allowlist already
        # auto-allows the same reads as command strings, so leaving these to
        # the tool's ASK default made the safer, structured path the one that
        # prompted.
        self._git_readonly_operations = frozenset(git_readonly_operations or [])
        # Populated by `attach_class_defaults` after the tool registry is
        # built. Survives `reload()` — yaml edits don't drop class defaults.
        self._class_defaults: dict[str, str] = {}
        # Tools a mode `baseline` must not reach — see `exempt_from_baseline`.
        self._baseline_exempt: set[str] = set()
        # workspace_root is the project root path. When set, `_path_posture`
        # rewrites absolute paths inside the workspace to forward-slash
        # relative form before prefix-matching `path_overrides`. Without
        # this, an absolute file_path argument bypasses the kernel-lockdown
        # DENY rules (which are written as relative prefixes like
        # `tesseract/brain/`).
        self._workspace_root = (
            Path(workspace_root).resolve() if workspace_root else None
        )
        # The operator's own documents. Empty only in a hand-built policy (unit
        # fixtures); `load_permission_policy` refuses a file without the block.
        self._workspace_documents: dict[str, Any] = workspace_documents or {}
        # The same statement for the cards that name no document. Empty only
        # in a hand-built policy; `load_permission_policy` refuses a file
        # without the block.
        self._workspace_cards: dict[str, Any] = workspace_cards or {}

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        m = mode.strip().lower()
        if m not in VALID_MODES:
            raise ValueError(f"unknown security_mode {mode!r}; expected one of {sorted(VALID_MODES)}")
        self._mode = m

    def reload(self, path: Path) -> None:
        """Re-read `permissions.yaml` and replace internal state
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
        self.custom_defaults = fresh.custom_defaults
        self.modes = fresh.modes
        self.path_overrides = fresh.path_overrides
        self._mode = fresh._mode
        self._workspace_root = fresh._workspace_root
        self._bash_readonly_allowlist = fresh._bash_readonly_allowlist
        self._bash_readonly_exact_allowlist = fresh._bash_readonly_exact_allowlist
        self._git_readonly_operations = fresh._git_readonly_operations
        self._workspace_documents = fresh._workspace_documents
        self._workspace_cards = fresh._workspace_cards

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

    def exempt_from_baseline(self, names: Iterable[str]) -> None:
        """Mark tools a mode `baseline` must not relax.

        A relaxed mode says "everything this app can do, it does", and the
        operator granting that is trusting THIS APP — including the tools a
        later update adds, which is the whole reason the baseline exists. A
        tool exposed by a remote MCP server is not that: it arrives from
        outside the app, its roster can change without an update, and its
        class declares ASK as an external-capability floor for exactly that
        reason. Without this the baseline would lift that floor silently,
        and a server could widen its own reach by adding a tool.

        `permissions.yaml` can still name such a tool and decide it either
        way — the operator's explicit word outranks the floor. Only the
        blanket baseline is held off.
        """
        self._baseline_exempt.update(names)

    def class_default(self, tool_name: str) -> str | None:
        """The tool class's declared baseline (None if the tool wasn't
        registered with a default — typically only happens in unit tests
        that bypass `build_tool_registry`)."""
        return self._class_defaults.get(tool_name)

    def get_posture(self, tool_name: str, tool_input: BaseModel) -> PermissionResult:
        return _to_result(self.resolve_posture(tool_name, tool_input.model_dump()))

    def resolve_posture(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Resolve effective posture string for a tool call. Walks
        bash-readonly-allowlist → git-readonly-operations → path → mode →
        yaml override → tool class default → ASK fallback."""
        posture = (
            self._bash_readonly_posture(tool_name, tool_input)
            or self._git_readonly_posture(tool_name, tool_input)
            or self._path_posture(tool_name, tool_input)
            or self._mode_and_default(tool_name)
        )
        if posture is None:
            logger.warning("tool %s has no posture entry — defaulting to ASK", tool_name)
            posture = "ask"
        return posture

    def default_posture(self, tool_name: str) -> str:
        """Mode-aware default posture for a tool, ignoring path overrides.
        Used by the Settings/tools view to render baseline behavior. Falls
        through mode override → yaml override → class default → mode
        baseline → ASK so a tool without a yaml entry still surfaces the
        posture its class declared."""
        return self._mode_and_default(tool_name) or DEFAULT_POSTURE

    def posture_without_mode(self, tool_name: str) -> str:
        """What this tool would resolve to with the mode taken out of it.

        The named layers in resolution order and nothing else: `custom:` for a
        tool the assistant wrote, then `tools:`, then the tool's own class
        default. Two callers need exactly this and each had written the chain
        out by hand — the Settings writer, to know when an override row has
        nothing left to add, and the tools route, to show what a row would be
        without the mode. The route's copy omitted `custom:`, so a custom tool
        the operator had set to AUTO was reported as ASK.
        """
        return (
            self.custom_defaults.get(tool_name)
            or self.tools_defaults.get(tool_name)
            or self._class_defaults.get(tool_name)
            or DEFAULT_POSTURE
        )

    def has_path_overrides(self, tool_name: str) -> bool:
        return bool(self.path_overrides.get(tool_name))

    def has_mode_override(self, tool_name: str) -> bool:
        """True when the current mode NAMES this tool. A mode baseline is not
        an override: it applies to every tool that was not singled out, so
        reporting it here would badge the whole roster and say nothing."""
        return self._mode_override(tool_name) is not None

    def mode_baseline(self) -> str | None:
        """The posture the current mode gives every tool it does not name,
        or None when the mode states itself tool by tool."""
        block = self.modes.get(self._mode) or {}
        val = block.get("baseline")
        return str(val).strip().lower() if val is not None else None

    def _bash_readonly_posture(self, tool_name: str, tool_input: dict[str, Any]) -> str | None:
        """`bash` calls whose `command` matches the configured
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

    def _git_readonly_posture(self, tool_name: str, tool_input: dict[str, Any]) -> str | None:
        """`git` calls that only read THIS project resolve AUTO regardless of
        mode, matching what `bash_readonly_allowlist` already grants the same
        reads spelled as a command. Returns None for every other tool, for any
        operation that writes, and for any repository outside the workspace, so
        those fall through as usual.

        **The `repo` check is what makes the parity real.** `bash` runs with
        `cwd=workspace_root` and its allowlist entries are bare prefixes, so
        `git log` through that door can only ever read this project. `GitTool`
        accepts a `repo` anywhere outside the sealed trees, which is most of
        the disk. Matching on the operation alone would therefore hand the
        model prompt-free reads of every repository on the machine and call it
        parity.

        The operation is a closed `Literal` on `GitInput`. That bounds which
        VERB runs, and bounds nothing about its arguments: `_argv` refuses a
        `ref` that could turn `show` into a write.
        """
        if tool_name != "git" or not self._git_readonly_operations:
            return None
        operation = str(tool_input.get("operation") or "").strip().lower()
        if not operation or operation not in self._git_readonly_operations:
            return None
        repo = str(tool_input.get("repo") or "").strip()
        if repo and not self._inside_workspace(repo):
            return None
        return "auto"

    def _inside_workspace(self, raw_path: str) -> bool:
        """True when `raw_path` resolves inside the workspace root.

        A workspace we do not know is not one a path can be inside of, so an
        unset root answers False and the caller prompts. Resolution failures
        answer False for the same reason.
        """
        if self._workspace_root is None:
            return False
        try:
            resolved = Path(raw_path).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return False
        return resolved == self._workspace_root or self._workspace_root in resolved.parents

    def _path_posture(self, tool_name: str, tool_input: dict[str, Any]) -> str | None:
        """The strictest answer across every write-side path this call carries.

        Three properties, and the first two were absent until a `file_copy`
        onto `workspace/SOUL.md` was measured resolving AUTO:

        1. **The fields checked are the tool's own write targets.**
           `WRITE_PATH_FIELDS` is the same map `decide.evaluate` bounds a
           write with, so the copy/move asymmetry is answered once: a copy's
           source is a read and only its destination is a write, a move's
           source is a write because taking the file away changes it. Reading
           `file_path`/`path` alone meant an override written for the transfer
           verbs matched nothing and still read as closed.
        2. **Strictest wins across the paths, not first.** A move whose source
           is protected and whose destination is not must not resolve on the
           destination. Within ONE path the first matching prefix still wins,
           which is what lets a specific rule precede the general one it sits
           under.
        3. A path that matches no rule contributes nothing rather than
           contributing AUTO, so it falls through to the mode the way it
           always did.
        """
        rules = self.path_overrides.get(tool_name) or []
        if not rules:
            return None
        fields = WRITE_PATH_FIELDS.get(tool_name, ("file_path", "path"))
        answers: list[str] = []
        for field in fields:
            path = str(tool_input.get(field) or "")
            if not path:
                continue
            path_norm = self._normalize_for_prefix_match(path)
            for rule in rules:
                prefix = str(rule.get("path_prefix", ""))
                if prefix and path_norm.startswith(prefix):
                    answers.append(str(rule.get("posture", "ask")).lower())
                    break
        if not answers:
            return None
        return min(answers, key=lambda p: _STRICTNESS.get(p, 0))

    def workspace_document_posture(self, document: str) -> str:
        """What a proposed change to ONE operator-owned document does now.

        `ask` files it in the workspace inbox and waits. `auto` applies it and
        files the same card already decided, so unattended operation costs
        speed rather than visibility. The one posture in this file a mode is
        allowed to move, which is why it is read here rather than through
        `path_overrides`: those are evaluated before the mode by design, and
        that is what keeps the sealed trees sealed in every mode.

        The mode states a baseline and the operator names the exceptions, the
        shape `modes:` already uses. Four properties hold together, and the
        resolver is written against all four rather than against whichever one
        a later change is about:

        1. A document the operator named asks, whatever the mode says.
        2. The map can only tighten. The answer is the STRICTER of the mode's
           baseline and the override, so a document cannot be moved to `auto`
           under a mode that says `ask` — an exception list that holds
           documents back must not double as a way to let one through.
        3. A document nobody named follows the baseline.
        4. `document` is required. A caller that did not know which file it
           was about would resolve the baseline and walk past the hold; both
           callers know, so there is no such call to serve.
        """
        block = self._workspace_documents.get("proposal") or {}
        value = block.get(self._mode)
        if value is None:
            raise ValueError(
                f"permissions.yaml: workspace_documents.proposal names no "
                f"posture for security mode {self._mode!r}. Every mode states "
                f"its own answer here rather than inheriting one"
            )
        baseline = str(value).strip().lower()
        held = self.workspace_document_holds.get(_document_name(document))
        if held is None:
            return baseline
        return min((baseline, held), key=lambda p: _STRICTNESS.get(p, 0))

    def workspace_card_posture(self, kind: str) -> str:
        """What a card of THIS kind does when nobody is watching.

        The other half of `workspace_document_posture`, and deliberately the
        same shape: a baseline per mode, and the kinds the operator holds back
        named as exceptions. A card that names one of the operator's documents
        is answered by that function; this one answers the rest, which had no
        policy to consult at all and so asked forever whatever the mode said.

        The four properties from the document resolver hold here unchanged:
        a kind the operator named asks whatever the mode says, the map can
        only tighten, a kind nobody named follows the baseline, and `kind` is
        required because a caller that did not know which card it held would
        resolve the baseline and walk past a hold.
        """
        block = self._workspace_cards.get("proposal") or {}
        value = block.get(self._mode)
        if value is None:
            raise ValueError(
                f"permissions.yaml: workspace_cards.proposal names no posture "
                f"for security mode {self._mode!r}. Every mode states its own "
                f"answer here rather than inheriting one"
            )
        baseline = str(value).strip().lower()
        held = self.workspace_card_holds.get(kind.strip())
        if held is None:
            return baseline
        return min((baseline, held), key=lambda p: _STRICTNESS.get(p, 0))

    @property
    def workspace_card_holds(self) -> dict[str, str]:
        """The card kinds the operator keeps a hand on, kind to posture."""
        return dict(self._workspace_cards.get("proposal_overrides") or {})

    @property
    def workspace_documents(self) -> tuple[str, ...]:
        """The operator-owned documents, in file order. The one list."""
        return tuple(self._workspace_documents.get("documents") or ())

    @property
    def workspace_document_holds(self) -> dict[str, str]:
        """The documents the operator keeps a hand on, name to posture.

        Exceptions only: a document that follows the mode is absent rather
        than restated, so the map cannot grow back into a per-document list
        that drifts from the baseline beside it.
        """
        return dict(self._workspace_documents.get("proposal_overrides") or {})

    def workspace_document_postures(self) -> dict[str, str]:
        """Every operator-owned document and what a proposal to it does now.

        One resolver for both surfaces. The Settings panel and the tool a
        phone calls read this rather than each joining the baseline to the
        override map, which is how two screens end up disagreeing about one
        file.
        """
        return {
            name: self.workspace_document_posture(name)
            for name in self.workspace_documents
        }

    def set_workspace_document_hold(self, document: str, must_ask: bool) -> None:
        """Hold one document back, or let it follow the mode again.

        The in-memory half of the change. The file is written by
        `permissions/workspace_holds.py::set_document_hold`, and the config
        watcher would reload it a moment later; this is here so the answer the
        operator just gave is in force for the next turn rather than for the
        one after the watcher fires.

        Letting a document go REMOVES its entry rather than writing the
        baseline into it, for the reason the tool overrides map does the same:
        an exception that restates the rule stops being an exception the next
        time the rule moves.
        """
        name = _document_name(document)
        if name not in self.workspace_documents:
            raise ValueError(
                f"{name} is not one of the operator's documents "
                f"({', '.join(self.workspace_documents)})"
            )
        holds = self._workspace_documents.setdefault("proposal_overrides", {})
        if must_ask:
            holds[name] = "ask"
        else:
            holds.pop(name, None)

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

    def _mode_override(self, tool_name: str) -> str | None:
        """The posture the current mode states for this tool BY NAME.

        Normalised the same way the load-time check normalises before
        validating it. They differed: validation compared `strip().lower()`
        while this read back `lower()` alone, so a quoted `" auto"` passed as
        valid and then matched nothing here, and the call fell to ASK with a
        warning. A value the file accepts has to be a value the resolver
        honours.
        """
        block = self.modes.get(self._mode) or {}
        overrides = block.get("overrides") or {}
        val = overrides.get(tool_name)
        return str(val).strip().lower() if val is not None else None

    def _mode_and_default(self, tool_name: str) -> str | None:
        """The mode layer folded together with the tool's own baseline.

        Three properties, held at once rather than one at a time:

        - a mode that names the tool decides it, as it always has;
        - a mode `baseline` reaches only the tools nothing else spoke for,
          so `free` is written as its exceptions and a tool added later is
          covered without anyone editing the mode;
        - a baseline never relaxes a DENY, nor a tool marked by
          `exempt_from_baseline`. `tools:` and the tool class are where a
          capability gets closed, and a mode is a convenience over that, not
          a way through it. Without this the resolver would read
          `baseline: auto` before the floor it sits above and silently
          reopen it.

        None means nothing had an opinion — the caller falls back to ASK.
        """
        explicit = self._mode_override(tool_name)
        if explicit is not None:
            return explicit
        # An entry in `custom:` IS the operator's answer for a tool the
        # assistant wrote, so no mode overrules it. Being named there is a
        # floor in its own right, the same way a DENY is: without that, setting
        # such a tool to ASK and then running under `free` resolved AUTO, which
        # is the override this block exists to prevent.
        chosen = self.custom_defaults.get(tool_name)
        if chosen is not None:
            return chosen
        underlying = (
            self.tools_defaults.get(tool_name)
            or self._class_defaults.get(tool_name)
        )
        if underlying == "deny" or tool_name in self._baseline_exempt:
            return underlying
        return self.mode_baseline() or underlying


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


def _mapping(raw: dict[str, Any], key: str, where: str = "") -> dict[str, Any]:
    """One value from the config, insisted upon as a mapping.

    Every block in this file that takes `name: value` pairs goes through here,
    because the same defect was fixed three times at three call sites before
    anyone wrote the fourth down: a scalar where a mapping belongs reaches
    `.items()` and raises AttributeError out of the loader, which no caller
    reporting a bad config catches, instead of a ValueError naming the key.

    Absent or null is a mapping of nothing, which every block treats as empty.
    """
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        label = f"{where}.{key}" if where else f"`{key}:`"
        raise ValueError(
            f"{label} is {value!r}; expected a mapping"
        )
    return value


def load_permission_policy(
    path: Path,
    workspace_root: str | None = None,
) -> PermissionPolicy:
    if not path.exists():
        raise FileNotFoundError(f"permissions config missing: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    mode = str(raw.get("security_mode") or "max").strip().lower()
    if mode not in VALID_MODES:
        raise ValueError(f"invalid security_mode {mode!r}; expected one of {sorted(VALID_MODES)}")

    # Shape before contents, through `_mapping`, for every block in this file
    # that takes pairs. Written once because it was written three times.
    tools = {k: str(v).strip().lower() for k, v in _mapping(raw, "tools").items()}
    for name, posture in tools.items():
        if posture not in _VALID_POSTURES:
            raise ValueError(f"invalid posture {posture!r} for tool {name!r}")

    # `custom:` names tools the assistant wrote, so its keys exist on ONE
    # machine and the shipped overlay carries the block empty. Validated the
    # same way `tools:` is: a posture is a posture wherever it is written.
    custom = {k: str(v).strip().lower() for k, v in _mapping(raw, "custom").items()}
    for name, posture in custom.items():
        if posture not in _VALID_POSTURES:
            raise ValueError(
                f"invalid posture {posture!r} for custom tool {name!r}"
            )
    both = sorted(set(custom) & set(tools))
    if both:
        raise ValueError(
            f"named in both `tools:` and `custom:`: {', '.join(both)}. A tool "
            "is the app's or it is yours, and two entries cannot both decide it"
        )

    modes = _mapping(raw, "modes")
    for mode_name, block in modes.items():
        if mode_name not in VALID_MODES:
            raise ValueError(f"unknown mode {mode_name!r} in permissions.yaml")
        # The shape first. Every other malformed value in this file raises a
        # ValueError naming its key; a scalar here (`free: auto`) used to reach
        # `.get` and raise AttributeError instead, which no caller reporting a
        # bad config catches.
        if block is not None and not isinstance(block, dict):
            raise ValueError(
                f"modes.{mode_name} is {block!r}; expected a mapping with "
                "`baseline` and/or `overrides`"
            )
        block = block or {}
        overrides = _mapping(block, "overrides", f"modes.{mode_name}")
        # Overrides are validated for EVERY mode, baseline or not. This sat
        # below the `baseline is None` skip, so `max` — the shipped mode, and
        # the one with no baseline — was the single mode whose overrides were
        # never checked.
        for tool_name, posture in overrides.items():
            if str(posture).strip().lower() not in _VALID_POSTURES:
                raise ValueError(
                    f"invalid posture {posture!r} for tool {tool_name!r} in "
                    f"modes.{mode_name}.overrides"
                )
            # A mode may not name a tool `custom:` has already decided. The
            # resolver reads the mode first, so such a pair would resolve to
            # the mode and quietly contradict the floor this block is
            # documented to be. Refusing the pair keeps the guarantee true
            # rather than softening the sentence that states it, and keeps a
            # mode able to name any tool that is not one the assistant wrote.
            # Guards the FILE, not the object. `PermissionPolicy.modes` and
            # `.custom_defaults` are plain dicts the Settings route mutates in
            # place, and nothing re-runs this after a write. The route cannot
            # produce the pair today — its custom branch returns before the
            # branch that writes a mode's overrides — so this is the whole
            # enforcement until a second writer exists, and a second writer
            # would need to bring its own.
            if tool_name in custom:
                raise ValueError(
                    f"modes.{mode_name}.overrides names {tool_name!r}, which "
                    "`custom:` already decides. A tool the assistant wrote is "
                    "answered by `custom:` alone, so remove one of the two"
                )
        baseline = block.get("baseline")
        if baseline is not None and str(baseline).strip().lower() not in _VALID_BASELINES:
            raise ValueError(
                f"modes.{mode_name}.baseline is {baseline!r}; expected one of "
                f"{sorted(_VALID_BASELINES)}"
            )

    path_overrides = _mapping(raw, "path_overrides")

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

    if "git_readonly_operations" not in raw:
        raise ValueError(
            "permissions.yaml missing required key 'git_readonly_operations' — "
            "the `git` tool's read-only carve-out has no config-defined list to "
            "consult. Add the key (may be an empty list) rather than relying on "
            "a hardcoded default."
        )
    git_readonly_operations = raw["git_readonly_operations"] or []
    if not isinstance(git_readonly_operations, list) or not all(
        isinstance(item, str) for item in git_readonly_operations
    ):
        raise ValueError(
            "permissions.yaml 'git_readonly_operations' must be a list of strings"
        )

    # Validated LAST of the required blocks, so a file missing several says so
    # in the order the reader added them rather than in the order this function
    # happens to check. Moving it earlier made every test of another missing
    # key report this one instead.
    workspace_documents = _load_workspace_documents(raw)
    workspace_cards = _load_workspace_cards(raw)
    _expand_workspace_documents(path_overrides, workspace_documents)

    return PermissionPolicy(
        tools_defaults=tools,
        custom_defaults=custom,
        modes=modes,
        path_overrides=path_overrides,
        current_mode=mode,
        workspace_root=workspace_root,
        bash_readonly_allowlist=bash_readonly_allowlist,
        bash_readonly_exact_allowlist=bash_readonly_exact_allowlist,
        git_readonly_operations=git_readonly_operations,
        workspace_documents=workspace_documents,
        workspace_cards=workspace_cards,
    )


_WORKSPACE_PREFIX = "workspace/"


def _load_workspace_documents(raw: dict[str, Any]) -> dict[str, Any]:
    """Read and validate the `workspace_documents` block.

    Required, and loud when it is wrong, because every branch below it is a
    decision about the operator's own files. A missing key here is not a
    default to fall back on: it is a list of protected documents that has
    silently become empty.
    """
    block = raw.get("workspace_documents")
    if not isinstance(block, dict):
        raise ValueError(
            "permissions.yaml missing required 'workspace_documents' block — "
            "it names the operator-owned documents, the posture the raw write "
            "verbs get on them, and what a proposed change does per mode"
        )
    documents = block.get("documents")
    if not isinstance(documents, list) or not documents or not all(
        isinstance(d, str) and d and "/" not in d for d in documents
    ):
        raise ValueError(
            "permissions.yaml 'workspace_documents.documents' must be a "
            "non-empty list of bare file names (no directory part)"
        )
    # One posture, for every document, in every mode. A map here would be a
    # per-document exception to the closed door, and the closed door is the
    # reason a proposal is the only way in. Refused by shape rather than left
    # to the posture check below, whose message would say `expected one of
    # ['ask', 'auto', 'deny']` about something that is not a posture at all.
    if isinstance(block.get("direct_write"), dict):
        raise ValueError(
            "permissions.yaml 'workspace_documents.direct_write' is a map. It "
            "takes one posture and no exceptions: the raw write verbs are "
            "closed on every one of these files so that a proposal is the "
            "only door, and a per-document exception here would reopen it"
        )
    direct = str(block.get("direct_write", "")).strip().lower()
    if direct not in _STRICTNESS:
        raise ValueError(
            f"permissions.yaml 'workspace_documents.direct_write' is "
            f"{block.get('direct_write')!r}; expected one of "
            f"{sorted(_STRICTNESS)}"
        )
    proposal = block.get("proposal")
    if not isinstance(proposal, dict):
        raise ValueError(
            "permissions.yaml 'workspace_documents.proposal' must be a map of "
            "security mode to posture"
        )
    by_mode = {k: v for k, v in proposal.items() if k != _OVERRIDES_KEY}
    missing = sorted(VALID_MODES - set(by_mode))
    if missing:
        raise ValueError(
            f"permissions.yaml 'workspace_documents.proposal' names no posture "
            f"for {missing}. Every mode states its own answer here rather than "
            f"inheriting one, so adding a mode is a decision about these files"
        )
    for mode_name, value in by_mode.items():
        if str(value).strip().lower() not in _STRICTNESS:
            raise ValueError(
                f"permissions.yaml 'workspace_documents.proposal.{mode_name}' "
                f"is {value!r}; expected one of {sorted(_STRICTNESS)}"
            )
    return {
        "documents": list(documents),
        "direct_write": direct,
        "proposal": {str(k): str(v).strip().lower() for k, v in by_mode.items()},
        "proposal_overrides": _load_proposal_overrides(proposal, documents),
    }


def _load_workspace_cards(raw: dict[str, Any]) -> dict[str, Any]:
    """Read and validate the `workspace_cards` block.

    Required and loud for the same reason its sibling is: the block decides
    what the runtime settles on the operator's behalf while nobody is looking,
    and a missing key is not a default to fall back on.

    An override naming a kind that does not exist is refused here rather than
    silently ignored, because a hold on a misspelled kind is a hold that does
    nothing and reads, in the file, exactly like one that works.
    """
    block = raw.get("workspace_cards")
    if not isinstance(block, dict):
        raise ValueError(
            "permissions.yaml missing required 'workspace_cards' block — it "
            "says what a card that names no document does per mode, and which "
            "kinds always wait for the operator"
        )
    proposal = block.get("proposal")
    if not isinstance(proposal, dict):
        raise ValueError(
            "permissions.yaml 'workspace_cards.proposal' must be a map of "
            "security mode to posture"
        )
    by_mode = {k: v for k, v in proposal.items() if k != _OVERRIDES_KEY}
    missing = sorted(VALID_MODES - set(by_mode))
    if missing:
        raise ValueError(
            f"permissions.yaml 'workspace_cards.proposal' names no posture for "
            f"{missing}. Every mode states its own answer here rather than "
            f"inheriting one"
        )
    for mode_name, value in by_mode.items():
        if str(value).strip().lower() not in _STRICTNESS:
            raise ValueError(
                f"permissions.yaml 'workspace_cards.proposal.{mode_name}' is "
                f"{value!r}; expected one of {sorted(_STRICTNESS)}"
            )

    from tesseract.workspace_events.events import DECIDABLE_KINDS

    overrides_raw = proposal.get(_OVERRIDES_KEY) or {}
    if not isinstance(overrides_raw, dict):
        raise ValueError(
            "permissions.yaml 'workspace_cards.proposal.overrides' must be a "
            "map of card kind to posture"
        )
    overrides: dict[str, str] = {}
    for kind, value in overrides_raw.items():
        name = str(kind).strip()
        if name not in DECIDABLE_KINDS:
            raise ValueError(
                f"permissions.yaml 'workspace_cards.proposal.overrides' holds "
                f"{name!r}, which is not a card anyone can decide. The kinds "
                f"are {sorted(DECIDABLE_KINDS)}"
            )
        posture = str(value).strip().lower()
        if posture not in _STRICTNESS:
            raise ValueError(
                f"permissions.yaml 'workspace_cards.proposal.overrides.{name}' "
                f"is {value!r}; expected one of {sorted(_STRICTNESS)}"
            )
        overrides[name] = posture
    return {
        "proposal": {str(k): str(v).strip().lower() for k, v in by_mode.items()},
        "proposal_overrides": overrides,
    }


#: Where the operator's per-document exceptions sit inside `proposal`, and the
#: one name the mode loop above must not read as a mode.
_OVERRIDES_KEY = "overrides"


def _document_name(path: str) -> str:
    """The bare file name of a workspace document, from any way of writing it.

    Callers hand this `workspace/OPERATING.md`, an absolute path on Windows,
    or the bare name. The block is keyed by bare names, so every one of those
    has to arrive at the same key or a hold is silently missed for one caller
    and honoured for another.
    """
    return str(path).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _load_proposal_overrides(
    proposal: dict[str, Any], documents: list[str]
) -> dict[str, str]:
    """The documents the operator keeps a hand on, validated.

    Loud about a name that is not one of the documents. An override on a file
    the block does not list holds nothing back, and it reads exactly like one
    that does: a typo in a file name is the difference between `OPERATING.md`
    being gated and it being written unattended, and nothing downstream can
    tell the two apart.
    """
    raw = proposal.get(_OVERRIDES_KEY)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            "permissions.yaml 'workspace_documents.proposal.overrides' must "
            "be a map of document name to posture, naming only the documents "
            "the operator keeps a hand on"
        )
    out: dict[str, str] = {}
    for name, value in raw.items():
        doc = str(name).strip()
        if doc not in documents:
            raise ValueError(
                f"permissions.yaml 'workspace_documents.proposal.overrides' "
                f"names {doc!r}, which is not one of the documents "
                f"({', '.join(documents)}). An exception on a file that is "
                f"not listed holds nothing back"
            )
        posture = str(value).strip().lower()
        if posture not in _STRICTNESS:
            raise ValueError(
                f"permissions.yaml 'workspace_documents.proposal.overrides."
                f"{doc}' is {value!r}; expected one of {sorted(_STRICTNESS)}"
            )
        out[doc] = posture
    return out


def _expand_workspace_documents(
    path_overrides: dict[str, list[dict[str, Any]]],
    block: dict[str, Any],
) -> None:
    """Give every write verb one list, and the same answer for the same path.

    Two things happen here, and they are the same principle applied twice.

    The workspace documents become rules, prepended. Prepended rather than
    appended because `_path_posture` takes the first matching prefix within a
    path, and a broader `workspace/` rule added later must not shadow them.

    Then every write verb gets the whole list, not just its own. The file was
    written with rules under `file_write` alone, so `file_copy` and
    `file_move` matched NOTHING: not the workspace documents, and not
    `config/`, `tools/` or the sealed source trees either. Two of those were
    covered a layer down by `file_write._check_runtime_lockdown`, which the
    transfer tools share, and the rest were not covered at all. A destination
    is a destination whichever verb is carrying the bytes there.

    This makes some answers stricter and some looser, and both directions are
    the point: a copy into `config/permissions.yaml` is now refused by policy
    rather than only by the tool, and a copy into `workshop/` proceeds exactly
    as a write there does. A verb that answered differently for the same
    target was the defect, not the strictness.
    """
    rules = [
        {
            "path_prefix": f"{_WORKSPACE_PREFIX}{name}",
            "posture": block["direct_write"],
            "reason": (
                "operator-owned document: the assistant proposes a change and "
                "the workspace inbox settles it, so no write verb reaches it"
            ),
        }
        for name in block["documents"]
    ]
    shared = rules + list(path_overrides.get("file_write") or [])
    for tool_name in WRITE_PATH_FIELDS:
        path_overrides[tool_name] = list(shared)
