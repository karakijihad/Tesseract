"""Agent definition loader.

Reads .md files from the agents/ directory. Each file has YAML frontmatter
(name, version, model_role, etc.) and named markdown sections (## Section Name).
Every agent must declare `model_role` in its frontmatter — there is no
default fallback. A missing `model_role` raises loudly at load time.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# `[^\S\n]*` rather than `\s*`: `\s` matches newlines too, so `\s*\n` lets a
# run of blank lines be split many ways and a card that never closes its
# frontmatter costs quadratic time. Horizontal whitespace is all the trailing
# space on a `---` line was ever meant to allow.
_FRONTMATTER_RE = re.compile(r"^---[^\S\n]*\n(.*?)\n---[^\S\n]*\n", re.DOTALL)
_SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)

from tesseract.paths import agents_dir as _home_agents_dir

# Distributable-app relocation (Task 3): the loader used to overlay
# TESSERACT_HOME/agents on top of TESSERACT_DIR/agents so built-ins
# stayed reachable without a bootstrap seed step. `config_seed.
# ensure_agents_seeded()` now copies every card (all built-ins included)
# into TESSERACT_HOME/agents at boot, so TESSERACT_HOME/agents is the
# single source of truth — a runtime fallback to the code tree would
# make it impossible to reason about which card is live.
_PENDING_DIRNAME = "pending"
_PROVISIONAL_DIRNAME = "provisional"
# Stage 10 — operator-rejected proposals are archived here. NEVER a load
# path: both subdirectory scans below must skip it, or a rejected agent
# would silently rejoin the active set.
_REJECTED_DIRNAME = "rejected"


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
        if not sub.is_dir() or sub.name in (
            _PENDING_DIRNAME, _PROVISIONAL_DIRNAME, _REJECTED_DIRNAME, "__pycache__",
        ):
            continue
        candidate = sub / f"{name}.md"
        if candidate.exists():
            return candidate
    return None


def load_agent(
    name: str,
    agents_dir: Path | None = None,
    include_pending: bool = False,
) -> AgentDefinition:
    """Load an agent definition by name from the agents directory.

    Looks for {name}.md in agents_dir (defaults to TESSERACT_HOME/agents,
    call-time resolved via `tesseract.paths.agents_dir()`) and in one level
    of named subdirectories (e.g. ``audits/``). When `include_pending=True`,
    also searches `agents_dir/pending/` (the W7-A quarantine, audit M6
    follow-up, 2026-04-29). Raises FileNotFoundError if nothing matches.
    """
    directory = agents_dir or _home_agents_dir()
    path = _find_agent_path(directory, name)
    if path is None and include_pending:
        pending_path = directory / _PENDING_DIRNAME / f"{name}.md"
        if pending_path.exists():
            path = pending_path
    if path is None:
        raise FileNotFoundError(
            f"Agent definition not found: {name!r} in {directory}"
            f"{' (pending also empty)' if include_pending else ''}"
        )

    raw = path.read_text(encoding="utf-8")

    # Parse frontmatter
    fm: dict = {}
    body = raw
    match = _FRONTMATTER_RE.match(raw)
    if match:
        try:
            fm = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            logger.warning("Failed to parse frontmatter for agent %s", name)
        body = raw[match.end():]

    # Split body into named sections
    section_positions = [(m.group(1).strip(), m.start(), m.end()) for m in _SECTION_RE.finditer(body)]
    sections: dict[str, str] = {}
    for i, (section_name, _, end) in enumerate(section_positions):
        next_start = section_positions[i + 1][1] if i + 1 < len(section_positions) else len(body)
        sections[section_name] = body[end:next_start].strip()

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
    )


def list_agents(agents_dir: Path | None = None, include_pending: bool = False) -> list[str]:
    """Return names of all registered agents.

    By default returns only active agents (.md files at the top of
    `agents_dir` and in one level of named subdirectories). When
    `include_pending=True`, also returns names of quarantined agents in
    `agents_dir/pending/` so the operator can see what's queued for
    promotion. The `agent_promote` tool consumes that listing. Active
    agents shadow pending names on collision.
    """
    directory = agents_dir or _home_agents_dir()
    seen: set[str] = set()
    active: list[str] = []
    if directory.exists():
        for p in sorted(directory.glob("*.md")):
            if p.name == "INDEX.md" or p.stem in seen:
                continue
            seen.add(p.stem)
            active.append(p.stem)
        for sub in sorted(directory.iterdir()):
            if not sub.is_dir() or sub.name in (
                _PENDING_DIRNAME, _PROVISIONAL_DIRNAME, _REJECTED_DIRNAME, "__pycache__",
            ):
                continue
            for p in sorted(sub.glob("*.md")):
                if p.name == "INDEX.md" or p.stem in seen:
                    continue
                seen.add(p.stem)
                active.append(p.stem)
    if not include_pending:
        return active
    pending_dir = directory / _PENDING_DIRNAME
    if not pending_dir.exists():
        return active
    pending = [
        p.stem for p in sorted(pending_dir.glob("*.md"))
        if p.name != "INDEX.md" and p.stem not in active
    ]
    return active + pending


def list_rejected_agents(agents_dir: Path | None = None) -> list[str]:
    """Return names of operator-rejected agents archived in ``rejected/``.

    Stage 10 — agent_create consults this for re-proposal dedup: a name the
    operator already rejected errors out (with the recorded reason) instead
    of silently re-entering the pending queue.
    """
    directory = agents_dir or _home_agents_dir()
    rejected_dir = directory / _REJECTED_DIRNAME
    if not rejected_dir.exists():
        return []
    return [
        p.stem for p in sorted(rejected_dir.glob("*.md"))
        if p.name != "INDEX.md"
    ]


def resolve_agent_path(name: str, agents_dir: Path | None = None, include_pending: bool = False) -> Path:
    """Return the resolved .md path for ``name``. Raises FileNotFoundError if absent."""
    directory = agents_dir or _home_agents_dir()
    found = _find_agent_path(directory, name)
    if found is not None:
        return found
    # The pending branch joins the name a SECOND time, so it needs the guard
    # too — `_find_agent_path` returning None is not evidence the name was safe.
    if include_pending and not _is_unsafe_agent_name(name):
        pending_path = directory / _PENDING_DIRNAME / f"{name}.md"
        if pending_path.exists():
            return pending_path
    raise FileNotFoundError(f"Agent definition not found: {name!r}")


def read_agent_source(name: str, agents_dir: Path | None = None, include_pending: bool = False) -> str:
    """Return the raw .md text for ``name`` (frontmatter + body)."""
    return resolve_agent_path(name, agents_dir, include_pending).read_text(encoding="utf-8")


def _validate_agent_source(name: str, source: str) -> None:
    """Parse ``source`` (frontmatter + body) and raise ``RuntimeError`` if the
    contract isn't met: must have frontmatter, must declare ``model_role``,
    ``tools`` if present must be a list. Mirrors ``load_agent``'s checks."""
    match = _FRONTMATTER_RE.match(source)
    if not match:
        raise RuntimeError(f"Agent {name!r}: missing YAML frontmatter")
    try:
        fm = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Agent {name!r}: frontmatter YAML invalid: {exc}") from exc
    if not isinstance(fm, dict):
        raise RuntimeError(f"Agent {name!r}: frontmatter must be a mapping")
    if not fm.get("model_role"):
        raise RuntimeError(f"Agent {name!r}: frontmatter must declare model_role")
    raw_tools = fm.get("tools")
    if raw_tools is not None and not isinstance(raw_tools, list):
        raise RuntimeError(
            f"Agent {name!r}: frontmatter `tools` must be a list (got {type(raw_tools).__name__})"
        )


def save_agent_source(name: str, source: str, agents_dir: Path | None = None) -> Path:
    """Atomically overwrite the agent .md after validating the new source.

    Raises ``RuntimeError`` if the new source fails frontmatter / model_role
    / tools-shape validation; the on-disk file is left unchanged.
    """
    _validate_agent_source(name, source)
    path = resolve_agent_path(name, agents_dir, include_pending=True)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(source, encoding="utf-8")
    import os as _os

    _os.replace(str(tmp), str(path))
    return path


def set_agent_disabled(name: str, disabled: bool, agents_dir: Path | None = None) -> Path:
    """Toggle the ``disabled`` flag in an agent's frontmatter and rewrite the
    file. Adds the field if absent. Atomic. Returns the .md path."""
    path = resolve_agent_path(name, agents_dir, include_pending=True)
    raw = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        raise RuntimeError(f"Agent {name!r}: missing YAML frontmatter")
    fm = yaml.safe_load(match.group(1)) or {}
    if not isinstance(fm, dict):
        raise RuntimeError(f"Agent {name!r}: frontmatter must be a mapping")
    fm["disabled"] = bool(disabled)
    new_fm_text = yaml.safe_dump(fm, sort_keys=False).rstrip() + "\n"
    body = raw[match.end():]
    new_source = f"---\n{new_fm_text}---\n{body}"
    return save_agent_source(name, new_source, agents_dir)


def list_pending_agents(agents_dir: Path | None = None) -> list[str]:
    """Return names of quarantined agents only — files in `pending/`."""
    directory = agents_dir or _home_agents_dir()
    pending_dir = directory / _PENDING_DIRNAME
    if not pending_dir.exists():
        return []
    return [
        p.stem for p in sorted(pending_dir.glob("*.md"))
        if p.name != "INDEX.md"
    ]
