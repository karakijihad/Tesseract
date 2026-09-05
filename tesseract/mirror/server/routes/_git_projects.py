"""The project registry, as the Git panel writes it.

Which directories are registered, which one the assistant is working on, and
which registration is gone. Nothing here touches a credential or an identity:
a row is a folder and a name, and the authority to run anything in it is
granted elsewhere.

Split out of ``settings_git.py``. The filesystem questions these routes ask
about a root belong to `_git_probes`, and are asked through it so that the
deadline and the one-probe-per-question sharing are the same ones the panel's
read path uses.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from aiohttp import web

from tesseract.mirror.server.routes import _git_probes as probes
from tesseract.mirror.server.routes._localhost import is_localhost_request


#: Distinguishes "no such project" from "here is why it cannot be opened",
#: without either colliding with a refusal string.
_UNREGISTERED = object()


def _project_body(body: Any) -> str | None:
    """The `id` from a request body, or ``None`` when there is not one."""
    if not isinstance(body, dict):
        return None
    project_id = body.get("id")
    if not isinstance(project_id, str) or not project_id.strip():
        return None
    return project_id.strip()


async def set_git_project_active(request: web.Request) -> web.Response:
    """POST /api/settings/git/active — choose the project the assistant works on.

    Until this existed the choice was reachable only through a tool call, so an
    operator looking at the panel could see which project was open and had no
    way to change it from there.

    **The panel hiding Open is not the guard.** `Git.tsx` offers Open only for
    a row whose folder is there, but that is a render, and the active project
    is what a lane takes as its working directory when nothing else names one.
    A check that lives only in the client is a check any other caller on this
    machine skips, including a client holding a table from before the folder
    was deleted, which is exactly the state AC-0 exists to end.
    """
    from tesseract.orchestrator.projects.store import (
        ProjectRootChanged,
        ProjectStore,
        ProjectStoreError,
        UnknownProjectError,
    )

    if not is_localhost_request(request):
        return web.json_response({"error": "localhost only"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    project_id = _project_body(body)
    if project_id is None:
        return web.json_response({"error": "id is required"}, status=400)

    store = ProjectStore()

    def _look_up_and_check() -> tuple[Any, str | None]:
        """`(refusal, root)`. A refusal of `None` means go ahead with `root`.

        The lookup and the check share a hop; the WRITE deliberately does not
        join them. This call runs under a deadline and can be abandoned while
        its thread is still going, and a call that may be abandoned must not be
        the one that mutates the registry, or a timeout could report a refusal
        over an activation that had already landed.

        The root it validated is carried out so the write can confirm the
        record still names it. `remove` frees an id and `register` can mint the
        same slug again, so the id alone does not identify what was checked.
        """
        known = store.get(project_id)
        if known is None:
            return _UNREGISTERED, None
        return probes.unopenable(known.root), known.root

    try:
        refusal, checked_root = await probes.under_stat_deadline(
            ("open", project_id),
            _look_up_and_check,
            default=(
                f"the folder registered for {project_id} did not answer in time",
                None,
            ),
        )
        if refusal is _UNREGISTERED:
            return web.json_response({"error": f"no project {project_id!r}"}, status=404)
        if refusal is not None or checked_root is None:
            return web.json_response({"error": refusal}, status=400)
        project = await asyncio.to_thread(
            store.set_active_if_root_matches, project_id, checked_root
        )
    except UnknownProjectError:
        return web.json_response({"error": f"no project {project_id!r}"}, status=404)
    except ProjectRootChanged as exc:
        # 409, not 500: nothing is broken and nothing was written. The registry
        # moved under a decision already made, and the answer is to ask again.
        return web.json_response({"error": str(exc)}, status=409)
    except ProjectStoreError as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"id": project.id, "name": project.name})


async def remove_git_project(request: web.Request) -> web.Response:
    """POST /api/settings/git/remove — drop a registration.

    **Deregisters only. The directory is never touched.** A registration is
    cheap to recreate and a folder of work is not, so the two must never share
    a button. A root that has gone missing is the common case here, and the
    operator wanting the row gone is not the operator wanting the tree gone.
    """
    from tesseract.orchestrator.projects.store import (
        ProjectStore,
        ProjectStoreError,
        UnknownProjectError,
    )

    if not is_localhost_request(request):
        return web.json_response({"error": "localhost only"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    project_id = _project_body(body)
    if project_id is None:
        return web.json_response({"error": "id is required"}, status=400)

    try:
        await asyncio.to_thread(ProjectStore().remove, project_id)
    except UnknownProjectError:
        return web.json_response({"error": f"no project {project_id!r}"}, status=404)
    except ProjectStoreError as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"removed": project_id})


async def register_git_project(request: web.Request) -> web.Response:
    """POST /api/settings/git/register — register a directory as a project.

    Localhost-gated like the other writers here, and for a sharper reason than
    they have: detection runs `git` inside the directory it is handed, so an
    unauthenticated caller would be choosing where a subprocess starts.

    `register` keys on the resolved root, so pointing this at a directory that
    is already registered updates that record in place rather than creating a
    second row for one tree (`store.py::register`).

    Verify commands are left empty and nothing is marked trusted. Detection can
    suggest a test command, but a suggested command is something the operator
    confirms, and this route has nobody to ask — `project_link` is the surface
    that does that conversation, and it is where standing trust is granted
    because an ASK-gated tool call is where the operator answered the trust
    question. Registering here adds a row and no authority; the registry stays
    the superset of `trusted_dirs.json` that `provisioning.py` describes.
    """
    from tesseract.orchestrator.projects.detect import detect_vcs
    from tesseract.orchestrator.projects.models import (
        Project,
        mint_project_id,
        normalize_root,
    )
    from tesseract.orchestrator.projects.store import ProjectStore, ProjectStoreError

    if not is_localhost_request(request):
        return web.json_response({"error": "localhost only"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be an object"}, status=400)

    raw_root = str(body.get("root") or "").strip()
    if not raw_root:
        return web.json_response({"error": "root is required"}, status=400)
    root = Path(raw_root).expanduser()
    if not root.is_absolute():
        return web.json_response(
            {"error": "root must be a full path, not a relative one"}, status=400
        )
    # Fails closed. A root whose status could not be established in time is
    # not registered on the assumption it is fine.
    refusal = await probes.under_stat_deadline(
        ("unopenable", str(root)),
        probes.unopenable,
        root,
        default=f"{root} did not answer in time to be checked",
    )
    if refusal is not None:
        return web.json_response({"error": refusal}, status=400)

    name = str(body.get("name") or "").strip() or root.name
    store = ProjectStore()
    try:
        taken = {p.id for p in await asyncio.to_thread(store.list_projects)}
        # Shells out to git, so it does not belong on the loop.
        vcs = await asyncio.to_thread(detect_vcs, root)
        project = Project(
            id=mint_project_id(name, taken),
            name=name,
            root=normalize_root(root),
            vcs=vcs,
        )
        saved = await asyncio.to_thread(store.register, project)
    except ProjectStoreError as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"id": saved.id, "name": saved.name, "root": saved.root})
