"""The Git panel: what version control can do on this machine, and as whom.

Its own module rather than another section of ``settings.py``, which already
held every unrelated panel in the app and was the wrong place for the identity
work to land on top of them. It then grew the same way, so the panel is four
modules and this one is the read: `get_git`, the single request that paints it.

* `_git_probes` — what this machine has (``git --version``, ``gh auth
  status``) and how long any question about a folder is given to answer;
* `_git_projects` — which directories are registered and which one is open;
* `_git_identity` — who git runs as, and the credential that record names.

Three questions, and the split between them is the whole design:

* **what this machine has** changes only when the machine is reconfigured, so
  it is cached and dropped by the events in `_git_identity` that change it;
* **what a project is** comes from the registry, which is read per request;
* **what a project's tree looks like right now** (``git status``) is genuinely
  live and is never cached.

The routes the app registers all live here as names, whichever module holds
the body: the panel is one URL prefix and one place to look it up.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from tesseract.mirror.server.routes import _git_probes as probes
from tesseract.mirror.server.routes._git_identity import (
    identity_view,
    connect_git_identity,
    disconnect_git_identity,
)
from tesseract.mirror.server.routes._git_projects import (
    register_git_project,
    remove_git_project,
    set_git_project_active,
)

log = logging.getLogger(__name__)

__all__ = [
    "connect_git_identity",
    "disconnect_git_identity",
    "get_git",
    "register_git_project",
    "remove_git_project",
    "set_git_project_active",
]


def _credential_readiness(identities: list[Any]) -> dict[str, bool] | None:
    """`{credential id: can be spent}`, or ``None`` if the store did not open.

    Not "holds a value". A blob sealed by another Windows account is a value
    that is stored and can never be sent, and this panel used to report it as
    a connected identity right up until the push failed.

    Read once for the whole page, and only when something actually names a
    credential: an operator who has connected nothing pays nothing for a
    question with no subject.
    """
    from tesseract.orchestrator.projects.models import AMBIENT

    wanted = {
        i.credential_ref for i in identities if i is not None and i.credential_ref != AMBIENT
    }
    if not wanted:
        return {}
    from tesseract.credentials.store import CredentialStore, CredentialStoreError

    try:
        # Narrowed to what is actually named. The map is read by id and a
        # store with fifty rows has nothing to say about an identity that
        # names one of them.
        return {
            row["id"]: row["spend_state"] == "ready"
            for row in CredentialStore().list_public()
            if row["id"] in wanted
        }
    except CredentialStoreError as exc:
        log.info("get_git: credential store unreadable (%s)", exc)
        return None


async def get_git(request: web.Request) -> web.Response:
    """GET /api/settings/git — whether version control is usable, and on what.

    Reports state; it grants nothing. Until this existed there was no way to
    see whether `gh` was signed in, as whom, or whether a project had a remote
    at all, so a push that failed on credentials read as a broken tool rather
    than a machine that was never signed in.

    **Every probe runs concurrently.** They are independent processes and the
    first version awaited them in a row: `gh auth status` alone takes about
    1.4s because it opens the system keyring, and the panel took 2.0s to paint
    behind the sum. Nothing here depends on anything else here.

    **The two machine-level probes are then cached** (`_MACHINE_PROBES`), so a
    warm open pays for `git status` and nothing else. `?fresh=1` re-reads them,
    which is what the panel's own "Check again" sends: a button that promised
    to check and served a cache would be the lie the cache exists to avoid.
    """
    from tesseract.credentials.redaction import redact_url_credentials as redact

    fresh = request.query.get("fresh") == "1"
    active_root: Path | None = None
    projects: list[Any] = []
    machine_identity: Any = None
    try:
        from tesseract.orchestrator.projects.store import ProjectStore

        # One read, not two. `store.py::snapshot` exists because a caller
        # taking the roster and the active project separately can straddle
        # a write and render a project the active-id resolution no longer
        # sees.
        store = ProjectStore()
        projects, active, machine_identity = store.snapshot()
        active_root = Path(active.root) if active else None
    except Exception as exc:  # noqa: BLE001 — reported, never fatal
        log.info("get_git: project registry unreadable (%s)", exc)
        active = None

    # One gather for every subprocess on the page. `git status` runs only for
    # the active project: it is a disk walk per repository, and a registry with
    # sixty projects would turn one panel into sixty walks for rows nobody is
    # looking at. The others report what the registry already recorded.
    pending: list[Any] = [probes._machine_probes(fresh=fresh)]
    if active_root is not None:
        pending.append(
            probes.probe(["git", "-C", str(active_root), "status", "--porcelain"])
        )
    # Joins the same gather rather than running before it: the roster is known
    # already, so nothing here depends on a probe's answer.
    alive_index = len(pending)
    pending.append(probes._roots_alive_bounded([str(p.root) for p in projects]))
    results = await asyncio.gather(*pending, return_exceptions=True)

    def unwrap(index: int) -> tuple[int, str]:
        if index >= len(results):
            return 1, ""
        value = results[index]
        if isinstance(value, BaseException):
            log.info("get_git: probe %d failed (%s)", index, value)
            return 1, ""
        return value

    machine = results[0]
    if isinstance(machine, BaseException):
        log.info("get_git: machine probes failed (%s)", machine)
        machine = ((1, ""), (1, ""))
    (git_code, git_out), (gh_code, gh_out) = machine
    gh = (
        probes._parse_gh_status(gh_out)
        if gh_out
        else {"account": None, "protocol": None, "scopes": []}
    )

    clean: bool | None = None
    if active_root is not None:
        status_code, status_out = unwrap(1)
        # None means the question could not be answered, which must not render
        # as a clean tree.
        clean = (status_out == "") if status_code == 0 else None

    alive_raw = results[alive_index]
    # An empty map means the stat pass itself failed, and every root then reads
    # as present. That is the safe direction: telling someone their project is
    # gone because one stat glitched is worse than saying nothing.
    alive: dict[str, bool] = alive_raw if isinstance(alive_raw, dict) else {}

    # Off the loop, like every other read on this page. The store is a file,
    # and a locked or slow one must not hold the panel that reports it.
    ready = await asyncio.to_thread(
        _credential_readiness, [machine_identity, *(p.vcs.identity for p in projects)]
    )
    rows = [
        {
            "id": p.id,
            "name": p.name,
            "root": str(p.root),
            "root_exists": alive.get(str(p.root), True),
            "active": active is not None and p.id == active.id,
            "is_repo": bool(p.vcs.git),
            "remote": redact(p.vcs.remote) if p.vcs.remote else None,
            "branch": p.vcs.default_branch,
            "clean": clean if (active is not None and p.id == active.id) else None,
            # The project's OWN answer, null when it has none. What it falls
            # back to is the machine identity beside it, and the panel says so
            # rather than this route flattening the two into one field nobody
            # can tell apart.
            "identity": identity_view(p.vcs.identity, ready),
        }
        for p in projects
    ]

    return web.json_response({
        "git_installed": git_code == 0,
        "git_version": git_out if git_code == 0 else None,
        "gh_installed": gh_code != 127,
        # `gh auth status` exits non-zero precisely when nobody is signed in,
        # which is the question being asked.
        "gh_authenticated": gh_code == 0,
        "gh_account": gh["account"],
        "gh_protocol": gh["protocol"],
        "gh_scopes": gh["scopes"],
        "identity": identity_view(machine_identity, ready),
        "projects": rows,
    })
