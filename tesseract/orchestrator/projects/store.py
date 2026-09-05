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

from .models import (
    GitIdentity,
    Project,
    Registry,
    mint_project_id,
    normalize_root,
    utc_now_iso,
)
from .paths import registry_path

# The registry is one JSON file under a read-modify-write cycle. This
# serializes in-process writers; it does NOT close the cross-process window,
# which the registry does not need — it is single-operator, single-machine
# state written by tool calls, not a hot path.
_WRITE_LOCK = threading.Lock()


class ProjectStoreError(RuntimeError):
    """The registry exists but could not be read as a registry."""


class ProjectRootChanged(RuntimeError):
    """The record moved to a different root between a check and the write."""


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
        """Write the registry, or raise the error every caller already handles.

        `atomic_write_text` re-raises the filesystem's own exception, so a
        locked file or a full disk reached callers as a bare `OSError` while
        they were catching `ProjectStoreError` and cleaning up after
        themselves. One of them had a credential to remove and did not.
        """
        try:
            atomic_write_text(
                self.path,
                registry.model_dump_json(indent=2) + "\n",
                prefix=".registry-",
            )
        except OSError as exc:
            raise ProjectStoreError(
                f"project registry could not be written at {self.path}: {exc}"
            ) from exc

    def register(self, project: Project) -> Project:
        """Add or update ``project``, keyed by resolved root.

        Root is the identity, not the id: registering the same directory twice
        updates the existing record in place — adopting its id and
        ``created_at`` — rather than leaving two entries pointing at one tree.

        The id a caller arrives with is a preference, not a demand. Every
        caller mints one through :func:`mint_project_id` against a
        :meth:`list_projects` read taken before it went off to detect the tree,
        and that read is stale by the time it gets here: two folders sharing a
        basename, registered at the same moment, both minted the same slug and
        the second was refused. Re-minting against the registry this method
        just loaded is the only read that cannot go stale, because the lock is
        held across it and the write.
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
                        # Detection answers what the tree IS and knows nothing
                        # about who pushes it, so every caller arrives with a
                        # blank identity. Taking the new `vcs` whole would
                        # therefore drop the operator's override every time a
                        # registered project was registered again, and the
                        # next commit would silently carry the machine's own
                        # name instead.
                        "vcs": project.vcs.model_copy(
                            update={
                                "identity": project.vcs.identity or existing.vcs.identity
                            }
                        ),
                    }
                )
            else:
                project_id = project.id
                if project_id in registry.projects:
                    project_id = mint_project_id(project.name, set(registry.projects))
                merged = project.model_copy(update={"id": project_id, "root": root})
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

    def set_active_if_root_matches(self, project_id: str, expected_root: str) -> Project:
        """`set_active`, refusing if the record no longer names `expected_root`.

        A caller that checks a root before activating it — the Settings panel
        asks whether the folder is there and outside the sealed tree — has a
        window between the check and the write. `remove` frees an id and
        `register` can mint the same slug again for a different tree, so the
        write could activate a root nobody checked.

        The comparison is in memory and shares this method's single lock
        acquisition with the write, so the window closes to nothing. It is
        deliberately NOT a re-check of the filesystem: `Path.is_dir()` on an
        unmounted drive blocks for as long as the OS takes, and running that
        inside `_WRITE_LOCK` would serialise every registry write in the
        process behind one stuck stat.
        """
        with _WRITE_LOCK:
            registry = self._load()
            project = registry.projects.get(project_id)
            if project is None:
                raise UnknownProjectError(
                    f"no project registered with id {project_id!r}"
                )
            if normalize_root(project.root) != normalize_root(expected_root):
                raise ProjectRootChanged(
                    f"{project_id!r} now points at {project.root!r}, not the "
                    f"{expected_root!r} that was checked; nothing was activated"
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

    def snapshot(self) -> tuple[list[Project], Project | None, GitIdentity | None]:
        """Roster, active project and machine identity from ONE read.

        Separate calls would parse the file two or three times and could
        straddle a write, so a caller rendering them together could show a
        project the active-id resolution no longer sees, or a machine identity
        from after the roster it is being rendered beside.
        """
        registry = self._load()
        projects = sorted(registry.projects.values(), key=lambda p: p.name.lower())
        active = (
            registry.projects.get(registry.active_id) if registry.active_id else None
        )
        return projects, active, registry.git_identity

    # ── who git runs as ─────────────────────────────────────────────────

    def set_identity(
        self, identity: GitIdentity, *, project_id: str | None = None
    ) -> GitIdentity:
        """Record who git runs as. ``project_id=None`` sets the registry-wide
        default; naming a project overrides it for that project only."""
        with _WRITE_LOCK:
            registry = self._load()
            if project_id is None:
                registry.git_identity = identity
            else:
                project = registry.projects.get(project_id)
                if project is None:
                    raise UnknownProjectError(
                        f"no project registered with id {project_id!r}"
                    )
                registry.projects[project_id] = project.model_copy(
                    update={"vcs": project.vcs.model_copy(update={"identity": identity})}
                )
            self._save(registry)
            return identity

    def clear_identity(self, *, project_id: str | None = None) -> GitIdentity | None:
        """Forget an identity record and return what was there.

        Returning the old record is what lets the caller decide about the
        credential it named: the store row outlives this call, and only the
        caller can see whether anything else still points at it.
        """
        with _WRITE_LOCK:
            registry = self._load()
            if project_id is None:
                previous = registry.git_identity
                registry.git_identity = None
            else:
                project = registry.projects.get(project_id)
                if project is None:
                    raise UnknownProjectError(
                        f"no project registered with id {project_id!r}"
                    )
                previous = project.vcs.identity
                registry.projects[project_id] = project.model_copy(
                    update={"vcs": project.vcs.model_copy(update={"identity": None})}
                )
            self._save(registry)
            return previous

    def identity_for_root(self, root: Path | str) -> GitIdentity | None:
        """The identity a git command in ``root`` runs as, from ONE read.

        A project's own record wins; otherwise the registry-wide one; a root
        nobody registered still gets the registry-wide one, because "the
        assistant's commits are the assistant's" is not a property only
        registered directories have.

        **One filesystem call, not one per project.** Every root this compares
        against was normalized by :meth:`register` on the way in, so the
        stored strings are already resolved and matching is string work. A
        resolve per registered project put the cost of the whole registry on
        a caller asking about one directory, and every one of those calls can
        block for as long as an unmounted share takes to answer.

        The second pass exists for a registry someone edited by hand, where a
        root may not be in normal form. It runs only when nothing matched,
        which is also the answer for an unregistered directory, so the common
        paths never reach it.
        """
        registry = self._load()
        target = normalize_root(root)
        for as_stored in (True, False):
            for project in registry.projects.values():
                known = project.root if as_stored else normalize_root(project.root)
                if known == target:
                    return project.vcs.identity or registry.git_identity
        return registry.git_identity

    def credential_refs(self) -> set[str]:
        """Every credential id any identity record names, ambient included.

        The caller filters ambient out; this reports what is written down.
        """
        registry = self._load()
        refs = {
            project.vcs.identity.credential_ref
            for project in registry.projects.values()
            if project.vcs.identity is not None
        }
        if registry.git_identity is not None:
            refs.add(registry.git_identity.credential_ref)
        return refs

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
    "ProjectRootChanged",
    "ProjectStore",
    "ProjectStoreError",
    "UnknownProjectError",
]
