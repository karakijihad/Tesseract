"""Agent definition loader.

Reads .md files from the agents/ directories. Each file has YAML frontmatter
(name, version, model_role, etc.) and named markdown sections (## Section Name).
Every agent must declare `model_role` in its frontmatter — there is no
default fallback. A missing `model_role` raises loudly at load time.

**Two roots, and the order between them is the whole ownership rule.** Shipped
cards live in the sealed app tree (`system_agents_dir()`) and are read, never
copied; cards the assistant built for the operator live in `home/agents/`
(`user_agents_dir()`). A user card of the same slug SHADOWS the system one,
and every surface that resolves a card can say which won.

A shadow may be whole or partial. `extends: <system-slug>` in the frontmatter
makes the system card the base and applies only the fields the shadow states —
which is what lets an operator disable a shipped agent without freezing its
prompt at today's wording. A shadow without `extends` replaces the card
outright and stops following updates, and the write path says so.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

# Imported as a module, not by name: a test that stands up a fake install
# needs to move both roots, and one monkeypatch on `tesseract.paths` moves
# them for every module that resolves through here.
from tesseract import paths

logger = logging.getLogger(__name__)

# `[^\S\n]*` rather than `\s*`: `\s` matches newlines too, so `\s*\n` lets a
# run of blank lines be split many ways and a card that never closes its
# frontmatter costs quadratic time. Horizontal whitespace is all the trailing
# space on a `---` line was ever meant to allow.
_FRONTMATTER_RE = re.compile(r"^---[^\S\n]*\n(.*?)\n---[^\S\n]*\n", re.DOTALL)
_SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)

_PENDING_DIRNAME = "pending"
_PROVISIONAL_DIRNAME = "provisional"
# Stage 10 — operator-rejected proposals are archived here. NEVER a load
# path: both subdirectory scans below must skip it, or a rejected agent
# would silently rejoin the active set.
_REJECTED_DIRNAME = "rejected"

#: Subdirectories a scan for ACTIVE agents must skip. Named once because two
#: scans below share it and a third reader lives outside this module —
#: `scripts/check_tool_claims.py` holds the workspace documents to the agent
#: roster, and a roster that included the quarantine would let a document name
#: a rejected agent and still pass, which is the one thing that check exists to
#: catch.
NON_LOAD_DIRNAMES: frozenset[str] = frozenset({
    _PENDING_DIRNAME, _PROVISIONAL_DIRNAME, _REJECTED_DIRNAME, "__pycache__",
})

AgentOrigin = Literal["system", "user"]

# The frontmatter key that makes a shadow partial. Only a user card may carry
# it, and only naming a system card — see `load_agent`.
_EXTENDS_KEY = "extends"


def _same_dir(a: Path, b: Path) -> bool:
    """Whether two agent roots are one directory.

    In a dev checkout `home_dir()` IS `TESSERACT_DIR`, so the user and system
    roots collide. Every merge below dedupes on this rather than assuming the
    two are distinct — otherwise a dev checkout lists all twenty-five cards
    twice and every one of them "shadows" itself.
    """
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def default_roots() -> tuple[Path, ...]:
    """The agent roots in resolution order: user first, then shipped.

    The order IS the shadowing rule, so it is stated once, here, rather than
    re-derived by each caller.
    """
    user, system = paths.user_agents_dir(), paths.system_agents_dir()
    return (user,) if _same_dir(user, system) else (user, system)


def _roots(agents_dir: Path | None) -> tuple[Path, ...]:
    """The roots a caller's `agents_dir` argument means.

    `None` is the live system: user root, then shipped root. Every caller that
    wants the card which will actually run passes `None`.

    An explicit directory is that directory ALONE — a test tree, a temp dir
    `agent_create` validates a draft in, or the user root when the caller
    means the operator's half specifically (quarantine, promotion, the
    rejected archive). No explicit directory is ever quietly widened, so a
    caller that names one gets exactly what it named.
    """
    return default_roots() if agents_dir is None else (agents_dir,)


def _origin_of(directory: Path) -> AgentOrigin:
    """Which half a root belongs to.

    A dev checkout's single root resolves to both, and it answers `"system"`:
    the cards there are the shipped ones, and calling them the operator's
    would let the fork-on-edit path below write a file onto its own source.
    """
    return "system" if _same_dir(directory, paths.system_agents_dir()) else "user"


@dataclass(frozen=True)
class AgentLocation:
    """Where a card was found and what that means.

    The one resolution primitive: `load_agent`, `resolve_agent_path` and both
    write paths go through it, so "which card is live" has a single answer
    that a surface can render rather than infer.
    """

    name: str
    path: Path
    origin: AgentOrigin
    # True only for a user card that covers a system slug — so a surface can
    # distinguish "the operator wrote this" from "the operator overrode ours".
    shadows_system: bool = False
    # The system slug a partial shadow extends, when it declares one.
    extends: str | None = None


@dataclass
class AgentDefinition:
    name: str
    model_role: str
    version: str = "0.1"
    max_tokens_override: int | None = None
    description: str = ""
    sections: dict[str, str] = field(default_factory=dict)
    # Optional whitelist of tool names exposed to the sub-session. None means
    # "use the caller's DEFAULT_TOOL_SUBSET". Empty list means "no tools".
    tools: list[str] | None = None
    # Operator-controlled enable flag (frontmatter `disabled: true|false`).
    # `invoke_agent` refuses to run a disabled agent. Defaults to False so
    # legacy agents without the field stay enabled.
    disabled: bool = False
    # Where the winning card was read from, and whether it covered a shipped
    # one. Set by `load_agent`; carried on the definition so a caller that
    # already has it does not have to resolve the path a second time to
    # answer "is this ours or theirs".
    origin: AgentOrigin = "user"
    shadows_system: bool = False

    def get_section(self, section_name: str) -> str:
        """Return section body by name, stripped. Empty string if missing."""
        return self.sections.get(section_name, "").strip()


def _is_unsafe_agent_name(name: str) -> bool:
    """Whether `name` could name a file outside the agents directory.

    An agent name reaches here straight off a URL segment
    (`/api/agents/{name}/source`, read AND write). aiohttp's default pattern
    excludes `/` and nothing else, which is not enough on Windows: `\\` is a
    separator there too, and `C:x` is drive-relative — NOT `is_absolute()`,
    yet joining it discards the agents directory entirely. The same character
    opens an NTFS alternate data stream.

    Checked here rather than at each join, so every caller of
    `_find_agent_path` and `resolve_agent_path` inherits it. Mirrors
    `pinned_fetch._unsafe_filename_reason`, which reasons this out at length
    for the same class of sink.
    """
    if not name or name in (".", ".."):
        return True
    if "/" in name or "\\" in name or ":" in name or "\x00" in name:
        return True
    return Path(name).name != name


def _find_agent_path(directory: Path, name: str) -> Path | None:
    """Search for ``{name}.md`` in ``directory`` and one level of subdirectories.

    Top-level match wins over a subdirectory match on name collision (built-in
    agents take priority). Subdirectory search supports grouped agent families
    such as ``audits/`` without requiring the caller to know the subfolder.
    Returns ``None`` when ``directory`` does not exist or no match is found.
    """
    if _is_unsafe_agent_name(name) or not directory.exists():
        return None
    top = directory / f"{name}.md"
    if top.exists():
        return top
    for sub in sorted(directory.iterdir()):
        if not sub.is_dir() or sub.name in NON_LOAD_DIRNAMES:
            continue
        candidate = sub / f"{name}.md"
        if candidate.exists():
            return candidate
    return None


def locate_agent(
    name: str,
    agents_dir: Path | None = None,
    include_pending: bool = False,
) -> AgentLocation | None:
    """Resolve ``name`` across the roots, or ``None`` if nothing matches.

    User root first, shipped root second, so a user card of the same slug
    wins — and the returned location says it did, which is what makes the
    shadowing visible instead of merely true.

    `pending/` is searched in the user root only. Quarantine is a property of
    the write side: nothing the app ships is ever awaiting the operator's
    promotion, and reading a shipped `pending/` would let an update slip an
    unpromoted card past the gate that exists to stop exactly that.
    """
    roots = _roots(agents_dir)
    for directory in roots:
        path = _find_agent_path(directory, name)
        if path is None:
            continue
        origin = _origin_of(directory)
        extends = _declared_extends(path) if origin == "user" else None
        return AgentLocation(
            name=name,
            path=path,
            origin=origin,
            shadows_system=(
                origin == "user" and _find_agent_path(paths.system_agents_dir(), name) is not None
            ),
            extends=extends,
        )
    if include_pending and not _is_unsafe_agent_name(name):
        pending_path = roots[0] / _PENDING_DIRNAME / f"{name}.md"
        if pending_path.exists():
            return AgentLocation(
                name=name, path=pending_path, origin=_origin_of(roots[0]),
            )
    return None


def _declared_extends(path: Path) -> str | None:
    """The `extends:` slug in ``path``'s frontmatter, if it declares one.

    Read on its own rather than as a by-product of a full load: the write
    paths need to know whether a shadow is partial before they parse anything
    else, and a malformed card must not make the question unanswerable.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        return None
    try:
        fm = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(fm, dict):
        return None
    value = fm.get(_EXTENDS_KEY)
    return str(value).strip() or None if value else None


def _parse_card(name: str, raw: str) -> tuple[dict, dict[str, str]]:
    """Split a card into its frontmatter mapping and its named sections."""
    fm: dict = {}
    body = raw
    match = _FRONTMATTER_RE.match(raw)
    if match:
        try:
            fm = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            logger.warning("Failed to parse frontmatter for agent %s", name)
        if not isinstance(fm, dict):
            fm = {}
        body = raw[match.end():]

    positions = [(m.group(1).strip(), m.start(), m.end()) for m in _SECTION_RE.finditer(body)]
    sections: dict[str, str] = {}
    for i, (section_name, _, end) in enumerate(positions):
        next_start = positions[i + 1][1] if i + 1 < len(positions) else len(body)
        sections[section_name] = body[end:next_start].strip()
    return fm, sections


def load_agent(
    name: str,
    agents_dir: Path | None = None,
    include_pending: bool = False,
) -> AgentDefinition:
    """Load an agent definition by name.

    Searches the user root then the shipped root (`default_roots()`), each at
    the top level and in one level of named subdirectories (e.g. ``audits/``).
    An explicit `agents_dir` restricts the search to that directory. When
    `include_pending=True`, also searches the user root's `pending/` (the W7-A
    quarantine, audit M6 follow-up, 2026-04-29). Raises FileNotFoundError if
    nothing matches.

    A user card declaring `extends: <slug>` is merged over the shipped card of
    that slug: the shipped sections and fields are the base, the shadow's
    stated fields win. That is what a disable written by the Mirror looks like
    — four lines, so the prompt keeps following the shipped card.
    """
    location = locate_agent(name, agents_dir, include_pending)
    if location is None:
        where = " or ".join(str(root) for root in _roots(agents_dir))
        raise FileNotFoundError(
            f"Agent definition not found: {name!r} in {where}"
            f"{' (pending also empty)' if include_pending else ''}"
        )

    fm, sections = _parse_card(name, location.path.read_text(encoding="utf-8"))

    if location.extends:
        base_path = _find_agent_path(paths.system_agents_dir(), location.extends)
        if base_path is None:
            raise RuntimeError(
                f"Agent {name!r}: extends {location.extends!r}, which is not a "
                "shipped agent. `extends` may only name a card in "
                f"{paths.system_agents_dir()}."
            )
        base_fm, base_sections = _parse_card(location.extends, base_path.read_text(encoding="utf-8"))
        if base_fm.get(_EXTENDS_KEY):
            raise RuntimeError(
                f"Agent {name!r}: extends {location.extends!r}, which itself "
                "extends another card. Shadowing is one hop deep."
            )
        # The shadow's own keys win; `extends` itself is consumed here and is
        # not part of the resulting definition.
        merged_fm = {**base_fm, **{k: v for k, v in fm.items() if k != _EXTENDS_KEY}}
        merged_fm.pop(_EXTENDS_KEY, None)
        fm, sections = merged_fm, {**base_sections, **sections}

    max_override = fm.get("max_tokens_override")
    if max_override is not None:
        try:
            max_override = int(max_override)
        except (TypeError, ValueError):
            max_override = None

    role = fm.get("model_role")
    if not role:
        raise RuntimeError(
            f"Agent {name!r}: frontmatter must declare model_role"
        )

    raw_tools = fm.get("tools")
    tools: list[str] | None
    if raw_tools is None:
        tools = None
    elif isinstance(raw_tools, list):
        tools = [str(t).strip() for t in raw_tools if str(t).strip()]
    else:
        raise RuntimeError(
            f"Agent {name!r}: frontmatter `tools` must be a list (got {type(raw_tools).__name__})"
        )

    return AgentDefinition(
        name=fm.get("name", name),
        model_role=role,
        version=str(fm.get("version", "0.1")),
        max_tokens_override=max_override,
        description=fm.get("description", ""),
        sections=sections,
        tools=tools,
        disabled=bool(fm.get("disabled", False)),
        origin=location.origin,
        shadows_system=location.shadows_system,
    )


def _active_in(directory: Path, seen: set[str], out: list[str]) -> None:
    """Append the active card names in ``directory`` to ``out``, skipping any
    stem already `seen`. Top level first, then one level of subdirectories."""
    if not directory.exists():
        return
    for p in sorted(directory.glob("*.md")):
        if p.name == "INDEX.md" or p.stem in seen:
            continue
        seen.add(p.stem)
        out.append(p.stem)
    for sub in sorted(directory.iterdir()):
        if not sub.is_dir() or sub.name in NON_LOAD_DIRNAMES:
            continue
        for p in sorted(sub.glob("*.md")):
            if p.name == "INDEX.md" or p.stem in seen:
                continue
            seen.add(p.stem)
            out.append(p.stem)


def list_agents(agents_dir: Path | None = None, include_pending: bool = False) -> list[str]:
    """Return names of all registered agents, user cards shadowing shipped
    ones of the same slug.

    Roots are walked in `default_roots()` order and a stem is taken once, so a
    shadow contributes one name rather than a duplicate. An explicit
    `agents_dir` lists that directory alone.

    When `include_pending=True`, also returns names of quarantined agents in
    the user root's `pending/` so the operator can see what's queued for
    promotion. The `agent_promote` tool consumes that listing. Active agents
    shadow pending names on collision.
    """
    roots = _roots(agents_dir)
    seen: set[str] = set()
    active: list[str] = []
    for directory in roots:
        _active_in(directory, seen, active)
    if not include_pending:
        return active
    pending_dir = roots[0] / _PENDING_DIRNAME
    if not pending_dir.exists():
        return active
    pending = [
        p.stem for p in sorted(pending_dir.glob("*.md"))
        if p.name != "INDEX.md" and p.stem not in active
    ]
    return active + pending


def list_agents_by_origin(
    agents_dir: Path | None = None,
) -> dict[AgentOrigin, list[str]]:
    """The same listing, split by which root the winning card came from.

    The Agents tab shows the operator's own cards and Autonomy shows the
    system catalog; both read this rather than filtering a merged list on a
    naming convention. A shadowed system slug appears under `"user"` only —
    the shipped card is not live, so listing it as system would advertise a
    card nothing will run.
    """
    roots = _roots(agents_dir)
    seen: set[str] = set()
    by_origin: dict[AgentOrigin, list[str]] = {"system": [], "user": []}
    for directory in roots:
        names: list[str] = []
        _active_in(directory, seen, names)
        by_origin[_origin_of(directory)].extend(names)
    return by_origin


def list_rejected_agents(agents_dir: Path | None = None) -> list[str]:
    """Return names of operator-rejected agents archived in ``rejected/``.

    Stage 10 — agent_create consults this for re-proposal dedup: a name the
    operator already rejected errors out (with the recorded reason) instead
    of silently re-entering the pending queue.
    """
    rejected_dir = _roots(agents_dir)[0] / _REJECTED_DIRNAME
    if not rejected_dir.exists():
        return []
    return [
        p.stem for p in sorted(rejected_dir.glob("*.md"))
        if p.name != "INDEX.md"
    ]


def resolve_agent_path(name: str, agents_dir: Path | None = None, include_pending: bool = False) -> Path:
    """Return the resolved .md path for ``name``. Raises FileNotFoundError if absent."""
    location = locate_agent(name, agents_dir, include_pending)
    if location is None:
        raise FileNotFoundError(f"Agent definition not found: {name!r}")
    return location.path


def read_agent_source(name: str, agents_dir: Path | None = None, include_pending: bool = False) -> str:
    """Return the raw .md text for ``name`` (frontmatter + body)."""
    return resolve_agent_path(name, agents_dir, include_pending).read_text(encoding="utf-8")


def _validate_agent_source(name: str, source: str) -> None:
    """Parse ``source`` (frontmatter + body) and raise ``RuntimeError`` if the
    contract isn't met: must have frontmatter, must declare ``model_role``,
    ``tools`` if present must be a list. Mirrors ``load_agent``'s checks.

    A card declaring ``extends`` may omit ``model_role`` — it inherits one
    from the card it extends, and demanding a copy here would be demanding the
    duplication the partial shadow exists to avoid. `load_agent` still raises
    if the merged result has no role, so nothing reaches a run without one.
    """
    match = _FRONTMATTER_RE.match(source)
    if not match:
        raise RuntimeError(f"Agent {name!r}: missing YAML frontmatter")
    try:
        fm = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Agent {name!r}: frontmatter YAML invalid: {exc}") from exc
    if not isinstance(fm, dict):
        raise RuntimeError(f"Agent {name!r}: frontmatter must be a mapping")
    if not fm.get("model_role") and not fm.get(_EXTENDS_KEY):
        raise RuntimeError(f"Agent {name!r}: frontmatter must declare model_role")
    raw_tools = fm.get("tools")
    if raw_tools is not None and not isinstance(raw_tools, list):
        raise RuntimeError(
            f"Agent {name!r}: frontmatter `tools` must be a list (got {type(raw_tools).__name__})"
        )


def _write_target(location: AgentLocation) -> tuple[Path, bool]:
    """Where a write for ``location`` lands, and whether it forks.

    A shipped card is not writable — `app/` is replaced wholesale on update,
    so a write there is lost at the next one even where the filesystem allows
    it. The write goes to the user root instead and the returned flag says so,
    because the operator's copy stops following updates from that moment and a
    caller that does not say that is hiding the cost of the edit.

    In a dev checkout the two roots are one directory, so there is nothing to
    fork to and nothing to lose: the card there IS the source, and the write
    lands on it in place, subdirectory and all.
    """
    if location.origin == "user":
        return location.path, False
    if _same_dir(paths.user_agents_dir(), paths.system_agents_dir()):
        return location.path, False
    return paths.user_agents_dir() / f"{location.name}.md", True


def _atomic_write(path: Path, source: str) -> None:
    import os as _os

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(source, encoding="utf-8")
    _os.replace(str(tmp), str(path))


def save_agent_source(
    name: str, source: str, agents_dir: Path | None = None,
) -> AgentLocation:
    """Atomically write the agent .md after validating the new source.

    Raises ``RuntimeError`` if the new source fails frontmatter / model_role
    / tools-shape validation; the on-disk file is left unchanged.

    Editing a shipped card writes a **full** shadow into the user root: the
    operator now owns that card and it no longer follows updates. The returned
    location reports `shadows_system`, which is how a surface tells them.
    """
    _validate_agent_source(name, source)
    location = locate_agent(name, agents_dir, include_pending=True)
    if location is None:
        raise FileNotFoundError(f"Agent definition not found: {name!r}")
    target, forked = _write_target(location)
    _atomic_write(target, source)
    if not forked:
        return location
    return AgentLocation(
        name=name,
        path=target,
        origin="user",
        shadows_system=True,
        extends=_declared_extends(target),
    )


def set_agent_disabled(
    name: str, disabled: bool, agents_dir: Path | None = None,
) -> AgentLocation:
    """Toggle the ``disabled`` flag for an agent. Adds the field if absent.

    On a card the operator owns this rewrites its frontmatter in place. On a
    shipped card it writes a **partial** shadow — `extends` plus the flag —
    rather than copying the card, so the prompt keeps following the shipped
    version. Enable/disable is the one control that stays tunable on system
    work, and forking a whole card to set one boolean would freeze precisely
    what this phase exists to unfreeze.
    """
    location = locate_agent(name, agents_dir, include_pending=True)
    if location is None:
        raise FileNotFoundError(f"Agent definition not found: {name!r}")

    _, forked = _write_target(location)
    if forked:
        shadow = (
            "---\n"
            f"name: {name}\n"
            f"{_EXTENDS_KEY}: {name}\n"
            f"disabled: {'true' if disabled else 'false'}\n"
            "---\n"
        )
        return save_agent_source(name, shadow, agents_dir)

    raw = location.path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        raise RuntimeError(f"Agent {name!r}: missing YAML frontmatter")
    fm = yaml.safe_load(match.group(1)) or {}
    if not isinstance(fm, dict):
        raise RuntimeError(f"Agent {name!r}: frontmatter must be a mapping")
    fm["disabled"] = bool(disabled)
    new_fm_text = yaml.safe_dump(fm, sort_keys=False).rstrip() + "\n"
    new_source = f"---\n{new_fm_text}---\n{raw[match.end():]}"
    return save_agent_source(name, new_source, agents_dir)


def list_pending_agents(agents_dir: Path | None = None) -> list[str]:
    """Return names of quarantined agents only — files in the user root's
    `pending/`. Nothing shipped is ever awaiting promotion."""
    pending_dir = _roots(agents_dir)[0] / _PENDING_DIRNAME
    if not pending_dir.exists():
        return []
    return [
        p.stem for p in sorted(pending_dir.glob("*.md"))
        if p.name != "INDEX.md"
    ]
