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


class GitIdentity(BaseModel):
    """Who the assistant is when it runs git, and what it pushes with.

    ONE shape for both cases the operator can be in. ``credential_ref`` is
    either :data:`AMBIENT` — this machine's own credential helper and GitHub
    sign-in, which is what git did before any of this existed — or the id of a
    row in the credential store. Nothing that consumes this record may branch
    on which: the difference is one extra config argument at the call site, and
    a second code path for "the operator's own GitHub" is the defect this plan
    exists to prevent.

    ``name`` and ``email`` are carried in both cases, so a connected identity
    authors commits as itself whether or not it also brought a token.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=1, max_length=320)
    credential_ref: str = Field(default="ambient", min_length=1, max_length=64)


#: ``credential_ref`` for an identity that pushes with whatever this machine
#: already has. Named rather than spelled at each comparison, because the two
#: places that read it are in different packages.
AMBIENT = "ambient"


class VcsInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    git: bool = False
    remote: str | None = None
    default_branch: str | None = None
    #: Overrides the registry-wide identity for this project only. ``None``
    #: means "whatever the registry says", which is what keeps the operator's
    #: own repositories pushing as the operator while the assistant's own
    #: workshop pushes as the assistant, with one mechanism.
    identity: GitIdentity | None = None


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
    # The URL the project is published at. The gate fetches it and expects a
    # 2xx, which is what lets "it is live" be a check that ran rather than a
    # sentence. Never a shell command: it is fetched, not executed.
    live: str | None = None

    @field_validator("live")
    @classmethod
    def _live_is_a_web_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if not cleaned.startswith(("http://", "https://")):
            raise ValueError(
                f"live must be an http(s) URL the gate can fetch, got {cleaned!r}"
            )
        return cleaned

    def is_empty(self) -> bool:
        return not any((self.test, self.typecheck, self.lint, self.live))

    def as_contract(self) -> str:
        """The declared checks as one comparable string, gate order.

        Written onto a task when it is accepted and compared against the live
        project when it closes, so a close can tell whether the checks it ran
        are the ones that were promised. One renderer for both sides, because
        two would eventually disagree about spacing and report every task as
        changed.
        """
        return "\n".join(
            f"{name}: {command}"
            for name, command in (
                ("test", self.test), ("typecheck", self.typecheck),
                ("lint", self.lint), ("live", self.live),
            )
            if command
        )


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    root: str
    vcs: VcsInfo = Field(default_factory=VcsInfo)
    verify: VerifyCommands = Field(default_factory=VerifyCommands)
    conventions_file: str | None = None
    # What a day's unattended work on this project may spend, in USD. None
    # is "nothing declared", which is what every project says until its
    # owner sets one; the morning row reads it, nothing else does.
    budget_usd: float | None = Field(default=None, ge=0)
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
    #: The identity every project uses unless it names its own. ``None`` is a
    #: machine nobody has connected, which behaves exactly as git did before
    #: this field existed.
    git_identity: GitIdentity | None = None


def budget_line(budget_usd: float | None) -> str:
    """A budget as the operator reads it, and the two empties say different things.

    Lives beside the field so every surface says it the same way: the proposal
    `project_new` posts, the ASK gate `project_budget` raises, and the roster
    `project_list` prints. `None` and `0` are opposite answers, and a renderer
    showing either as "0.00" would hide the one that matters, which is that
    nothing was ever priced.
    """
    if budget_usd is None:
        return "none, so it will not be worked unattended"
    return f"${budget_usd:.2f} a day"


__all__ = [
    "AMBIENT",
    "GitIdentity",
    "Project",
    "Registry",
    "VcsInfo",
    "VerifyCommands",
    "budget_line",
    "mint_project_id",
    "normalize_root",
    "utc_now_iso",
]
