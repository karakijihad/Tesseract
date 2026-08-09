"""Read/write of ``<TESSERACT_HOME>/projects/registry.json``.

A corrupt or unreadable registry raises rather than resolving to an empty one.
Two reasons: a silent empty registry would let ``register`` clobber every
project the operator had, and the prompt block's failure marker only appears if
something tells it the registry is broken.
"""

from __future__ import annotations

import threading
from pathlib import Path

from pydantic import ValidationError

from tesseract.lib.yaml_io import atomic_write_text

from .models import Project, Registry, normalize_root, utc_now_iso
from .paths import registry_path

# The registry is one JSON file under a read-modify-write cycle. This
# serializes in-process writers; it does NOT close the cross-process window,
# which the registry does not need — it is single-operator, single-machine
# state written by tool calls, not a hot path.
_WRITE_LOCK = threading.Lock()


class ProjectStoreError(RuntimeError):
    """The registry exists but could not be read as a registry."""


class UnknownProjectError(KeyError):
    """No project with that id is registered."""

    def __str__(self) -> str:  # KeyError's repr quotes the message
        return self.args[0] if self.args else super().__str__()


class ProjectStore:
    """Registry access. Resolves its path at call time unless one is injected.

    ``path=None`` is the production shape: every operation re-resolves
    ``TESSERACT_HOME``, so a test that redirects the env after constructing a
    store still gets the scratch tree.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else registry_path()

    def _load(self) -> Registry:
        path = self.path
        if not path.exists():
            return Registry()
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProjectStoreError(f"project registry unreadable at {path}: {exc}") from exc
        try:
            return Registry.model_validate_json(raw)
        except ValidationError as exc:
            raise ProjectStoreError(f"project registry at {path} is malformed: {exc}") from exc

    def _save(self, registry: Registry) -> None:
        atomic_write_text(
            self.path,
            registry.model_dump_json(indent=2) + "\n",
            prefix=".registry-",
        )

    def register(self, project: Project) -> Project:
        """Add or update ``project``, keyed by resolved root.

        Root is the identity, not the id: registering the same directory twice
        updates the existing record in place — adopting its id and
        ``created_at`` — rather than leaving two entries pointing at one tree.
        An id collision against a *different* root is an error; the caller
        mints ids through :func:`mint_project_id` against
        :meth:`list_projects` and should not be inventing them.
        """
        with _WRITE_LOCK:
            registry = self._load()
            root = normalize_root(project.root)
            existing = next(
                (p for p in registry.projects.values() if normalize_root(p.root) == root),
                None,
            )
            if existing is not None:
                merged = project.model_copy(
                    update={
                        "id": existing.id,
                        "root": root,
                        "created_at": existing.created_at,
                        "last_active_at": project.last_active_at or existing.last_active_at,
                    }
                )
            else:
                clash = registry.projects.get(project.id)
                if clash is not None:
                    raise ProjectStoreError(
                        f"project id {project.id!r} is already registered against "
                        f"{clash.root!r}; mint a fresh id"
                    )
                merged = project.model_copy(update={"root": root})
            registry.projects[merged.id] = merged
            self._save(registry)
            return merged

    def set_active(self, project_id: str) -> Project:
        """Make ``project_id`` the active project and stamp ``last_active_at``."""
        with _WRITE_LOCK:
            registry = self._load()
            project = registry.projects.get(project_id)
            if project is None:
                raise UnknownProjectError(
                    f"no project registered with id {project_id!r}"
                )
            project = project.model_copy(update={"last_active_at": utc_now_iso()})
            registry.projects[project_id] = project
            registry.active_id = project_id
            self._save(registry)
            return project

    def active(self) -> Project | None:
        """The active project, or ``None`` when none is selected.

        An ``active_id`` naming a project that is no longer registered reads as
        "none selected" rather than raising — a stale pointer is a missing
        selection, not a broken registry.
        """
        registry = self._load()
        if registry.active_id is None:
            return None
        return registry.projects.get(registry.active_id)

    def get(self, project_id: str) -> Project | None:
        return self._load().projects.get(project_id)

    def list_projects(self) -> list[Project]:
        """Every registered project, name-sorted for stable operator output."""
        return self.snapshot()[0]

    def snapshot(self) -> tuple[list[Project], Project | None]:
        """Roster and active project from ONE read.

        Two calls would parse the file twice and could straddle a write, so a
        caller rendering both could show a project the active-id resolution no
        longer sees.
        """
        registry = self._load()
        projects = sorted(registry.projects.values(), key=lambda p: p.name.lower())
        active = (
            registry.projects.get(registry.active_id) if registry.active_id else None
        )
        return projects, active

    def remove(self, project_id: str) -> None:
        """Drop a project. Clears ``active_id`` when it pointed here."""
        with _WRITE_LOCK:
            registry = self._load()
            if project_id not in registry.projects:
                raise UnknownProjectError(
                    f"no project registered with id {project_id!r}"
                )
            registry.projects.pop(project_id)
            if registry.active_id == project_id:
                registry.active_id = None
            self._save(registry)


__all__ = [
    "ProjectStore",
    "ProjectStoreError",
    "UnknownProjectError",
]
