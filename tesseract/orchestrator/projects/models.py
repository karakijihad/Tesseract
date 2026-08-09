"""Persisted shapes for the project registry.

``extra="forbid"`` throughout: ``registry.json`` is hand-editable operator
state, and a typo'd key that parses into nothing would silently strip a
project's verify command or git identity. Failing the load is louder — the
prompt block renders a visible marker rather than a project missing half its
contract.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_root(root: Path | str) -> str:
    """Resolved-absolute string form, matching ``trust.py::_normalize``.

    The two must agree: the registry stores what ``mark_trusted`` keyed on, so
    a registered project reads back as trusted rather than re-prompting.
    """
    return str(Path(root).resolve())


def mint_project_id(name: str, taken: set[str] | None = None) -> str:
    """``proj-<slug>``, suffixed until it is free of ``taken``."""
    slug = _SLUG_STRIP.sub("-", name.strip().lower()).strip("-") or "project"
    candidate = f"proj-{slug}"
    existing = taken or set()
    if candidate not in existing:
        return candidate
    n = 2
    while f"{candidate}-{n}" in existing:
        n += 1
    return f"{candidate}-{n}"


class VcsInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    git: bool = False
    remote: str | None = None
    default_branch: str | None = None


class VerifyCommands(BaseModel):
    """The contract the verification gate consumes.

    Every command is stored as a plain string and executed through the normal
    bash policy path — never spliced into a subprocess here. A project cannot
    register a destructive command as its "test" and have it auto-run;
    ``bash_security``'s checks fire on it like any other command.
    """

    model_config = ConfigDict(extra="forbid")

    test: str | None = None
    typecheck: str | None = None
    lint: str | None = None

    def is_empty(self) -> bool:
        return not any((self.test, self.typecheck, self.lint))


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    root: str
    vcs: VcsInfo = Field(default_factory=VcsInfo)
    verify: VerifyCommands = Field(default_factory=VerifyCommands)
    conventions_file: str | None = None
    created_at: str = Field(default_factory=utc_now_iso)
    last_active_at: str | None = None

    @field_validator("root")
    @classmethod
    def _root_is_absolute(cls, value: str) -> str:
        """registry.json is hand-editable, so this is a load-time check.

        `register` normalizes every root it writes, but a hand-written `"."`
        parses fine and is then handed to `LaneManager.open` verbatim — where
        the seal check resolves it against the daemon's own working directory
        rather than the project's, so a dev launch spawns the lane wherever the
        backend happened to start. Refusing at load makes the prompt block
        render its visible marker instead of a lane opening somewhere nobody
        chose.
        """
        if not value.strip():
            raise ValueError("project root must not be blank")
        if not Path(value).is_absolute():
            raise ValueError(
                f"project root must be absolute, got {value!r} — a relative "
                "root resolves against whichever directory the process started in"
            )
        return value


class Registry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active_id: str | None = None
    projects: dict[str, Project] = Field(default_factory=dict)


__all__ = [
    "Project",
    "Registry",
    "VcsInfo",
    "VerifyCommands",
    "mint_project_id",
    "normalize_root",
    "utc_now_iso",
]
