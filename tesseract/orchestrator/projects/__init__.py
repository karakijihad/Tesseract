"""Project registry — what "the thing we are working on" is.

A registered project records where it lives, how it verifies itself, and its
git identity. The verification commands are the contract the verify gate
consumes; the root is what lanes default their working directory to.

State lives under ``<TESSERACT_HOME>/projects/registry.json`` — operator-private
per-machine, never under ``tesseract/`` and never in the production tree.
"""

from __future__ import annotations

from .models import Project, Registry, VcsInfo, VerifyCommands, mint_project_id
from .paths import projects_dir, registry_path
from .store import ProjectStore, ProjectStoreError, UnknownProjectError

__all__ = [
    "Project",
    "ProjectStore",
    "ProjectStoreError",
    "Registry",
    "UnknownProjectError",
    "VcsInfo",
    "VerifyCommands",
    "mint_project_id",
    "projects_dir",
    "registry_path",
]
