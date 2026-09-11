"""Tools the operator's own tree carries, loaded beside the shipped ones.

`<home>/tools/*.py`, discovered the way `<home>/agents/*.md` already is. A tool
written here is callable in the session that wrote it, and no part of that
needs building: `ToolRegistry` is a live dict (`brain/tools.py`),
`ChatSession._tool_schemas` reads it fresh on every request, and `tool_search`
resolves against it at call time. Registering an instance IS "no restart".

An update replaces `app/` and leaves `home/` alone, so a tool here also
survives every version. That is the whole point of the directory: the workshop
is where work in progress lives, and this is where a finished piece of it goes
on being usable.

Two rules the shipped tools do not need:

**A home tool may not take a shipped tool's name.** Agent cards shadow by slug
on purpose and that is safe, because a card is a prompt. A tool is the thing
the permission gate is named after, so a home `bash` or `file_write` would be
a posture lookup pointing at code the operator's tree supplied. Refused, with
the reason reported rather than logged and forgotten.

**A malformed one is skipped, never fatal.** `boot.py::_wire_tool_defaults`
raises on a shipped tool missing `default_posture`, and should: that is our
bug, caught before anything runs. The same fault in a home tool arrives while
a conversation is open, where raising takes the chat down over a file the
operator can fix in a second. Same checks, same messages, different blast
radius. This is how the config watcher already treats a broken yaml.

**Delete the file and the tool is gone.** No restart, no tombstone. An
in-flight call is unaffected, because `execute_tool` resolves the name once
and holds the object, so dropping the registry entry stops the next call
without touching the running one.

Everything here is tagged `origin = "custom"` on the instance, and that tag is
the whole separation. One registry, one list of N tools; filter it and you have
the operator's. The glossary and the shipped documents exclude them, Settings
and the usage heatmap and the working set show them marked.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import logging
import sys
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tesseract.kernel.tools.base import (
    VALID_RISK_CLASSES,
    Tool,
    check_tool_contract,
)
from tesseract.kernel.tools.recovery import UNSAFE
from tesseract.paths import user_tools_dir

log = logging.getLogger(__name__)

_MODULE_PREFIX = "tesseract_home_tools"


@dataclass(frozen=True)
class _Loaded:
    """What one file put into the registry, and the bytes that did it.

    `digest` rather than `st_mtime_ns`: editors save atomically by writing a
    temp file and renaming, some restore timestamps, and two saves inside the
    same clock tick are ordinary. A hash cannot be fooled by any of those, and
    reading a file the operator just wrote is not a cost worth optimising.

    `names` is what makes deletion and renaming work at all. Without it a
    vanished path is a path we know nothing about, so its tools would stay
    registered forever.
    """

    digest: str
    names: tuple[str, ...]


#: Absolute path -> what it loaded last time.
_loaded: dict[Path, _Loaded] = {}

#: The carried set the last scan applied. Lets an unchanged scan skip walking
#: the registry to rewrite tiers that are already right.
_last_carried: frozenset[str] | None = None

#: One scan at a time. Two `tool_search` calls land on separate worker threads,
#: and without this both would see the same changed file, both import it, both
#: pass the name check before either had registered, and one instance would be
#: silently discarded along with whatever its constructor did. Only ever held
#: on a worker thread, never on the event loop.
_scan_lock = threading.Lock()


@dataclass(frozen=True)
class LoadReport:
    """What one scan did. Every field is names, so a caller can say it out
    loud without reaching back into the module."""

    loaded: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    refused: tuple[str, ...] = ()
    #: Files that would not load. Each names a tool that is not callable.
    errors: tuple[str, ...] = ()
    #: A problem with the scan itself, not with any file. Separate because the
    #: consequence is the opposite: nothing was lost, nothing was read.
    unreadable: str = ""

    def summary(self) -> str:
        parts: list[str] = []
        if self.loaded:
            parts.append(f"loaded {', '.join(self.loaded)}")
        if self.removed:
            parts.append(f"removed {', '.join(self.removed)}")
        if self.refused:
            parts.append(f"refused {', '.join(self.refused)}")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        if self.unreadable:
            parts.append(self.unreadable)
        return "; ".join(parts) or "no change"


def _import_file(path: Path, digest: str) -> Any:
    """Execute `path` as a module under a stable, namespaced name.

    The name is registered in `sys.modules` before execution because pydantic
    and dataclasses resolve annotations through it; a module that is not there
    yet fails to build its own input schema. It is removed again if execution
    raises, so a broken file leaves nothing half-imported behind.
    """
    # Revisioned, not just the stem. Two files could otherwise share a module
    # name across reloads, and a stale entry in `sys.modules` would have the
    # previous revision's classes answering for the current file. The digest is
    # handed in from the scan's own read rather than read again here, so the
    # revision the name claims is the revision that was hashed.
    name = f"{_MODULE_PREFIX}.{path.stem}_{digest[:12]}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"no import machinery for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _tool_classes(module: Any) -> list[type[Tool]]:
    """Concrete `Tool` subclasses this file DEFINED.

    `obj.__module__ == module.__name__` is what keeps an imported symbol out:
    a home tool that does `from tesseract.kernel.tools.bash import BashTool`
    to read its shape would otherwise register a second copy of it.
    """
    found: list[type[Tool]] = []
    for obj in vars(module).values():
        if (
            inspect.isclass(obj)
            and issubclass(obj, Tool)
            and obj is not Tool
            and obj.__module__ == module.__name__
            and not inspect.isabstract(obj)
        ):
            found.append(obj)
    return found


def _contract_error(tool: Tool) -> str | None:
    """The boot gate's checks, returning instead of raising.

    Deliberately the same order and the same wording as
    `boot.py::_wire_tool_defaults`, so a tool author who moves a file from
    `<home>/tools/` into the kernel reads one message, not two.
    """
    cls = type(tool)
    posture = getattr(cls, "default_posture", "")
    if posture not in ("auto", "ask", "deny"):
        return (
            f"declares default_posture={posture!r}; expected one of "
            "('auto','ask','deny')"
        )
    risk = getattr(cls, "risk_class", "")
    if risk not in VALID_RISK_CLASSES:
        return (
            f"declares risk_class={risk!r}; expected one of "
            f"{sorted(VALID_RISK_CLASSES)}"
        )
    tier = getattr(tool, "tier", "")
    if tier not in ("core", "extended"):
        return f"resolves tier={tier!r}; expected one of ('core','extended')"
    # `depends_on` is required of a KERNEL tool, where boot raises and the
    # author is us. A tool in `<home>/tools/` is the operator's, and refusing
    # to load one written before the field existed would take a working tool
    # away to enforce a declaration about a dependency it may not even have.
    # So it defaults, loudly: the tool runs exactly as it did yesterday, with
    # no breaker, and the line says how to give it one.
    if not any("depends_on" in klass.__dict__ for klass in cls.__mro__ if klass is not Tool):
        cls.depends_on = ""
        log.warning(
            "custom tool %r does not say what it depends on, so nothing can "
            "notice when that is down. Add `depends_on: ClassVar[str] = "
            '"role:<roles.yaml key>"` or `"service:<providers.yaml services '
            'key>"` to its class, or `""` if it needs nothing outside this '
            "machine.",
            tool.name,
        )
    # Same carve-out as `depends_on` above, and for the same reason. A kernel
    # tool that says nothing about repetition is our omission and boot raises;
    # one in `<home>/tools/` is the operator's, and refusing it would take a
    # working tool away over a field that did not exist when they wrote it.
    # The default takes nothing away either: `unsafe` is what recovery already
    # does with anything it cannot reason about, so the tool behaves exactly as
    # it did yesterday and the line says how to say otherwise.
    if not any(
        "recovery_behaviour" in klass.__dict__ for klass in cls.__mro__ if klass is not Tool
    ):
        cls.recovery_behaviour = UNSAFE
        log.warning(
            "custom tool %r does not say what a crash-recovery pass may do "
            "with it, so an interrupted call will stop and ask you rather "
            "than resume. Add `recovery_behaviour: ClassVar[str] = "
            '"read_only"` if it changes nothing outside the app, '
            '"idempotent" if running it again with the same arguments is the '
            'same effect rather than a second one, or "queryable" if '
            "something can be asked what happened first.",
            tool.name,
        )
    try:
        check_tool_contract(tool)
    except Exception as exc:
        return str(exc)
    return None


def _is_custom(registry: Any, name: str) -> bool:
    """True when `name` is a registered CUSTOM tool, so it may be retired.

    The guard that keeps the retire path off shipped tools. It cannot happen
    through the normal route (a custom tool can never take a shipped name) but
    it would be the worst bug in this module if it ever did.
    """
    existing = registry.tools.get(name)
    return existing is not None and getattr(existing, "origin", "shipped") == "custom"


def _load_one(
    path: Path,
    registry: Any,
    own: frozenset[str] = frozenset(),
    staged: dict[str, Tool] | None = None,
    digest: str = "",
) -> tuple[list[Tool], list[str], list[str]]:
    """Instantiate and vet every tool in one file. Never raises."""
    tools: list[Tool] = []
    refused: list[str] = []
    errors: list[str] = []
    try:
        module = _import_file(path, digest)
    except Exception as exc:
        return [], [], [f"{path.name}: import failed: {exc}"]
    except BaseException as exc:
        # `KeyboardInterrupt` and `SystemExit` from a plugin's import are worth
        # containing (a file calling `sys.exit()` must not take the runtime
        # with it) but not worth burying beside a routine syntax error.
        log.error(
            "home_tools: %s raised %s at import",
            path.name,
            type(exc).__name__,
            exc_info=True,
        )
        return [], [], [f"{path.name}: import raised {type(exc).__name__}"]

    classes = _tool_classes(module)
    if not classes:
        return [], [], [f"{path.name}: defines no Tool subclass"]

    for cls in classes:
        try:
            tool = cls()
        except Exception as exc:
            errors.append(f"{path.name}: {cls.__name__}() failed: {exc}")
            continue
        problem = _contract_error(tool)
        if problem is not None:
            errors.append(f"{path.name}: tool '{tool.name}' {problem}")
            continue
        # Registered, staged by an earlier file in this same scan, or already
        # taken by an earlier class in this file.
        taken = (
            registry.get(tool.name)
            or (staged or {}).get(tool.name)
            or next((t for t in tools if t.name == tool.name), None)
        )
        # `own` is what this same file registered last time. Editing a tool in
        # place must be a reload, not a collision: the old instance is still
        # in the registry holding the name, and refusing there would mean a
        # tool could be written once and never corrected.
        if taken is not None and tool.name not in own:
            where = (
                "one of your own tools in another file"
                if getattr(taken, "origin", "shipped") == "custom"
                else f"a tool TESSERACT ships ({type(taken).__name__})"
            )
            refused.append(tool.name)
            errors.append(
                f"{path.name}: '{tool.name}' is already the name of {where}, "
                "so this one is not loaded. Rename it."
            )
            continue
        tool.origin = "custom"  # type: ignore[attr-defined]
        tools.append(tool)
    return tools, refused, errors


def sync_home_tools(
    registry: Any,
    policy: Any = None,
    *,
    tools_dir: Path | None = None,
) -> LoadReport:
    """Register any new or changed tool under `<home>/tools/`.

    Safe to call on every `tool_search`: an unchanged directory costs one
    `scandir` and one `stat` per file. Safe to call at boot, where it runs
    before `_apply_tool_tiers` so a home tool picks up its tier and faces
    `_wire_tool_defaults` like any other.

    **A custom tool lands at ASK whatever it declares.** A shipped tool's
    `default_posture` is a claim we reviewed; a custom one is a claim the file
    makes about itself, and a file declaring `auto` beside
    `risk_class: autonomous` would be granting itself unattended execution.
    So the floor merged into the policy is ASK, and raising it is a separate
    act by the operator in `permissions.yaml`, which overrides class defaults
    and therefore already wins. Same freedom, switch on the operator's side.

    Callers that do not hold the policy (`tool_search` has a registry and no
    policy on its `ToolContext`) get it off the registry, where boot stashes it
    for the same reason it stashes the vault librarian.
    """
    with _scan_lock:
        return _sync_locked(registry, policy, tools_dir)


def _sync_locked(registry: Any, policy: Any, tools_dir: Path | None) -> LoadReport:
    if policy is None:
        policy = getattr(registry, "permission_policy", None)
    directory = tools_dir or user_tools_dir()
    try:
        if not directory.is_dir():
            # Deleting the whole folder has to retire what it held, exactly as
            # deleting one file does. But `is_dir()` also answers False when the
            # path cannot be read at all, and a permission blip or a network
            # home going away for a moment must not unregister every tool the
            # operator has. Absent means gone; unreadable means wait.
            if directory.exists() or not directory.parent.is_dir():
                return LoadReport(
                    unreadable=(
                        f"{directory} cannot be read just now, so nothing "
                        "changed. Everything already loaded is still there."
                    )
                )
            candidates = []
            root = directory
        else:
            root = directory.resolve()
            candidates = sorted(
                p
                for p in directory.iterdir()
                if p.is_file()
                and p.suffix == ".py"
                and not p.name.startswith("_")
                # A symlink pointing out of the tools directory would import
                # code the operator never approved into this directory, and
                # the ASK on the write is the only approval this lane has.
                and p.resolve().parent == root
            )
    except OSError as exc:
        return LoadReport(unreadable=f"{directory} could not be listed: {exc}")

    loaded: list[str] = []
    removed: list[str] = []
    refused: list[str] = []
    errors: list[str] = []
    class_defaults: dict[str, str] = {}
    promoted = promoted_names(tools_dir)
    seen: set[Path] = set()
    # Staged, then applied in one swap at the end. This runs on a worker
    # thread (`tool_search` calls it through `asyncio.to_thread`) while the
    # event loop is building schema payloads and rendering the glossary out of
    # the same dict. Mutating it in place would let a concurrent turn hit
    # "dictionary changed size during iteration"; rebinding a finished copy
    # cannot, because an iteration already under way holds the old dict and
    # nothing touches that one again.
    adds: dict[str, Tool] = {}
    drops: set[str] = set()

    for path in candidates:
        resolved = path.resolve()
        seen.add(resolved)
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            errors.append(f"{path.name}: unreadable: {exc}")
            continue
        previous = _loaded.get(resolved)
        # The digest says the FILE is unchanged. It says nothing about whether
        # this registry holds what the file produced: `_loaded` is a module
        # global and a registry is per build, so a second
        # `build_tool_registry` in the same process (the agent controller
        # rebuilds one on every tools reload) would skip every unchanged file
        # and come up with no custom tools at all.
        if (
            previous is not None
            and previous.digest == digest
            and all(name in registry.tools for name in previous.names)
        ):
            # The FILE is unchanged, and the floor it gets does not depend
            # on its contents anyway: the operator's own answer lives in
            # `permissions.yaml::custom`, which the resolver reads ahead of the
            # slot this writes. Skipping here used to skip the line below
            # too. The floor is carried forward for every name the file
            # produced, which is what the reload path would have set.
            for name in previous.names:
                class_defaults[name] = "ask"
            continue

        own = frozenset(previous.names if previous else ())
        tools, file_refused, file_errors = _load_one(
            path, registry, own, adds, digest
        )
        refused.extend(file_refused)
        errors.extend(file_errors)

        if file_errors and previous is not None and not tools:
            # A file that WAS good and now will not load keeps serving its last
            # good version. An editor saving a half-written line should not take
            # away a working tool; the error is reported instead, and the next
            # save is read because the digest moved.
            _loaded[resolved] = _Loaded(digest=digest, names=previous.names)
            continue

        # Retire what this file used to own before installing what it owns now,
        # so a renamed tool leaves under its old name in the same pass.
        for name in own:
            if name not in {t.name for t in tools} and _is_custom(registry, name):
                drops.add(name)
                adds.pop(name, None)
                removed.append(name)

        for tool in tools:
            # The tier is decided here, not left to the class body. Two
            # reasons: `_apply_tool_tiers` runs only at boot, so a tool loaded
            # by a live `tool_search` would otherwise keep whatever its file
            # declared forever; and a file declaring `tier = "core"` would be
            # putting itself on every turn, which is the operator's call and
            # is recorded in `promoted.txt`.
            tool.tier = "core" if tool.name in promoted else "extended"
            adds[tool.name] = tool
            drops.discard(tool.name)
            # ASK whatever the FILE claims, because the file is written by
            # the assistant and a tool that could declare its own silence
            # would be granting itself the thing the gate exists to ask
            # about. What this sets is the FLOOR; the operator's own answer
            # lives in `permissions.yaml::custom`, which the resolver reads
            # ahead of this slot and which the assistant cannot write.
            class_defaults[tool.name] = "ask"
            loaded.append(tool.name)
        _loaded[resolved] = _Loaded(
            digest=digest, names=tuple(t.name for t in tools)
        )

    # Files that are gone. Their tools go with them, which is what "delete it
    # and that is that" has to mean. An in-flight call is unaffected:
    # `execute_tool` resolved the name once and holds the object, so dropping
    # the registry entry stops the next call without touching the running one.
    for gone in [p for p in _loaded if p not in seen]:
        for name in _loaded[gone].names:
            if _is_custom(registry, name):
                drops.add(name)
                adds.pop(name, None)
                removed.append(name)
        del _loaded[gone]

    if adds or drops:
        updated = dict(registry.tools)
        for name in drops:
            updated.pop(name, None)
        updated.update(adds)
        registry.tools = updated

    # Re-tier EVERY registered custom tool, not only the ones rebuilt above:
    # the digest gate skips a file whose bytes have not changed, so promoting a
    # tool by editing `promoted.txt` alone reached nothing, and that file's own
    # header says it is safe to edit by hand.
    #
    # Skipped when nothing could have moved. This writes `tier` on shared
    # instances from a worker thread while the event loop may be reading it to
    # build a payload; the write cannot raise, but a payload assembled across
    # it can carry tiers from both sides. Doing it only when the promotions or
    # the registry actually changed keeps that window rare instead of opening
    # it on every search.
    global _last_carried
    try:
        carried = effective_core_names(registry)
    except (OSError, ValueError, KeyError):
        # No readable working set (a bare test registry, a half-built home).
        # The promotions alone are the best answer available, and it is the
        # same one this used to give.
        carried = promoted
    # Read once, because the block below assigns `_last_carried` and a second
    # reader after it would compare `carried` against itself and never see a
    # change.
    something_changed = bool(adds or drops or carried != _last_carried)
    if something_changed:
        _last_carried = carried
        for name, tool in registry.tools.items():
            if getattr(tool, "origin", "shipped") == "custom":
                tool.tier = "core" if name in carried else "extended"

    if policy is not None:
        if class_defaults:
            policy.merge_class_defaults(class_defaults)
        # Every custom tool the REGISTRY holds, not only the names this pass
        # happened to compute a posture for. A mode that auto-allows what it is
        # not told about would hand exactly the silence the comment above
        # refuses to a tool the assistant wrote, and a pass where every file
        # errored used to leave `class_defaults` empty and skip this with it.
        # The exemption is about which tools it covers, not about what this
        # scan managed to read.
        # Gated on the same condition the tier block above uses. The set update
        # is idempotent, so running it on a scan that changed nothing produces
        # no wrong state, only a walk of every registered tool per search —
        # and `tool_search` calls this on each one. Boot exempts independently,
        # so a quiet scan has nothing to add.
        exempt = getattr(policy, "exempt_from_baseline", None)
        if exempt is not None and something_changed:
            exempt(
                name
                for name, tool in registry.tools.items()
                if getattr(tool, "origin", "shipped") == "custom"
            )

    report = LoadReport(
        loaded=tuple(loaded),
        removed=tuple(removed),
        refused=tuple(refused),
        errors=tuple(errors),
    )
    if report.loaded:
        log.info("home_tools: %s", report.summary())
    for message in report.errors:
        log.warning("home_tools: %s", message)
    return report


def reset_load_cache() -> None:  # noqa: D401
    """Forget what has been loaded. For tests, which point `TESSERACT_HOME` at
    a fresh tmp dir per case and would otherwise inherit the previous one's
    records."""
    global _last_carried
    _loaded.clear()
    _last_carried = None


#: Custom tools the operator wants in the always-visible set, one name per
#: line. Beside the tools rather than in `working_set.yaml`, and not because
#: of where config lives: `generate_working_set --write` rewrites that file
#: whenever any tool is added or renamed, and it ships. A machine-local name
#: there is destroyed by the next generator run or handed to strangers as a
#: tool that does not exist. Here it is deleted with the folder, which is what
#: "custom is yours" has to mean.
PROMOTED_FILENAME = "promoted.txt"

#: Postures for custom tools used to live beside this, in `postures.txt`.
#: They are `permissions.yaml::custom` now: that file is DENY to the
#: `file_write` tool and this directory is only ASK, so the record of what a
#: self-written tool may do sat behind the weaker of the two gates. The reason
#: it was ever separate, that `permissions.yaml` ships verbatim, ended when
#: `config/_shipping/permissions.yaml` became what ships.
#:
#: Note what the DENY binds: tool calls. `_import_file` below executes a home
#: tool's module as ordinary Python, which can write any file the process can.
#: The real checkpoint is the ASK an operator answers to put the file in this
#: directory at all. The scan still sets the ASK floor; only the answer moved.


def promoted_path(tools_dir: Path | None = None) -> Path:
    return (tools_dir or user_tools_dir()) / PROMOTED_FILENAME


def promoted_names(tools_dir: Path | None = None) -> frozenset[str]:
    """Custom tool names the operator has promoted. Never raises.

    Unlike `working_set.yaml::core`, an unknown name here is ignored with a
    warning rather than refused. That file is ours and a typo in it is our
    bug, worth stopping boot for. This one holds names of files the operator
    may delete at any moment, and a promoted tool they removed must not be
    able to stop the app from starting.
    """
    path = promoted_path(tools_dir)
    try:
        if not path.is_file():
            return frozenset()
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.warning("home_tools: %s unreadable: %s", path, exc)
        return frozenset()
    names = {
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    }
    return frozenset(names)


def write_promoted(names: Iterable[str], tools_dir: Path | None = None) -> None:
    """Replace the promotions list. Sorted, so a diff of it reads."""
    path = promoted_path(tools_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(sorted({n.strip() for n in names if n.strip()}))
    header = (
        "# Custom tools carried on every turn, one name per line.\n"
        "# Written by Conscience -> Usage -> Working set. Safe to edit by hand.\n"
    )
    path.write_text(f"{header}{body}\n" if body else header, encoding="utf-8")


def effective_core_names(registry: Any) -> frozenset[str]:
    """Every tool carried on each turn: the shipped list plus custom promotions.

    One function, because this rule was written three times and the copies
    disagreed. The GET handler and the POST response answered differently about
    the same promotion, and `_apply_tool_tiers` answered differently again for a
    custom name someone had hand-added to `working_set.yaml`.

    Filtered to what is REGISTERED and, for the custom half, to what is
    actually custom: a shipped name in `promoted.txt` must not be carried
    through the operator's lane, and a promoted tool they have since deleted
    must not be counted as riding a turn it cannot ride.
    """
    from tesseract.config.working_set import load_core_tool_names

    shipped = set(load_core_tool_names())
    custom = {
        name
        for name in promoted_names()
        if getattr(registry.tools.get(name), "origin", "shipped") == "custom"
    }
    return frozenset((shipped | custom) & set(registry.tools))
