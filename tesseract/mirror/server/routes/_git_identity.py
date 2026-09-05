"""Who git runs as, and the credential that record names.

The whole identity lifecycle: reading a record for the panel, validating a
connect form, claiming and filling the credential row, writing the repository's
own `user.name` and `user.email`, and undoing all of it on disconnect.

Split out of ``settings_git.py`` because it is one transaction rather than a
set of endpoints: every step below is ordered against the ones around it so
that a failure anywhere leaves a state the panel can describe truthfully. The
module is longer than most here for that reason, and splitting it further
would put the two halves of one undo across two files.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web
from pydantic import ValidationError

from tesseract.credentials.models import CredentialDraft, Injection
from tesseract.mirror.server.routes import _git_probes as probes
from tesseract.mirror.server.routes._localhost import is_localhost_request

log = logging.getLogger(__name__)


def identity_view(identity: Any, ready: dict[str, bool] | None) -> dict[str, Any] | None:
    """One identity record as the panel reads it, or ``None`` for unconnected.

    `credential_ref` is null for an identity that pushes with this machine's
    own credentials, so a surface renders "who" and "what it pushes with"
    without knowing the word the registry stores. `credential_ready` is null
    when there is nothing to be ready — and also when the store could not be
    read, because "no value stored" and "the store did not open" are different
    news and only one of them is fixed by typing a token in again.
    """
    from tesseract.orchestrator.projects.models import AMBIENT

    if identity is None:
        return None
    ambient = identity.credential_ref == AMBIENT
    return {
        "name": identity.name,
        "email": identity.email,
        "credential_ref": None if ambient else identity.credential_ref,
        "credential_ready": (
            None
            if ambient or ready is None
            else ready.get(identity.credential_ref, False)
        ),
    }


# ── who git runs as ──────────────────────────────────────────────────────
#
# One flow, whichever account the operator is using. The panel asks one
# question — who does the assistant push as — and the answer is one record:
# a name, an email, and either this machine's own credentials or a token of
# its own. Everything below writes that record; nothing below branches on
# which of the two it is, and nothing that reads it does either.


#: The last resort if the models ever stop declaring their own bounds. Reached
#: only by `_max_length` below, which prefers the real thing.
_FALLBACK_MAX = 120


def _max_length(model: Any, field: str) -> int:
    """The store's own bound on one field, read rather than copied here.

    A number repeated in two files is a number that goes out of step, and the
    half that would drift is the one telling a person what to type.

    Never raises. This runs at import, and a route module that raises on
    import takes the whole backend's route registration with it: an internal
    question about a pydantic model would become a machine that does not
    start. `test_the_form_bounds_match_the_store` is where a change to the
    models is caught, in front of whoever makes it.
    """
    for rule in model.model_fields[field].metadata:
        if hasattr(rule, "max_length"):
            return int(rule.max_length)
    return _FALLBACK_MAX


_MAX_HOST = _max_length(CredentialDraft, "service")
_MAX_ACCOUNT = _max_length(Injection, "name")


def _identity_body(body: Any) -> tuple[dict[str, Any] | None, str | None]:
    """`(fields, refusal)` for a connect request.

    Every refusal names the field, because this form is the one place an
    operator types a token, and "invalid request" in front of a value they
    cannot see again is how a person loses one.

    That includes the ones the STORE bounds. A host longer than the credential
    record allows used to be refused by pydantic, whose message is a class
    name and a rule number: correct, unreadable, and about a field the person
    can see and shorten.
    """
    if not isinstance(body, dict):
        return None, "body must be an object"

    scope = body.get("scope")
    if scope is not None and (not isinstance(scope, str) or not scope.strip()):
        return None, "scope must be a project id, or null for this machine"

    name = str(body.get("name") or "").strip()
    email = str(body.get("email") or "").strip()
    if not name:
        return None, "name is required: it is what commits are authored by"
    if not email:
        return None, "email is required: git refuses to commit without one"

    credential = body.get("credential")
    if credential is not None:
        if not isinstance(credential, dict):
            return None, "credential must be an object, or null to use this machine's own"
        account = str(credential.get("account") or "").strip()
        token = str(credential.get("token") or "").strip()
        host = str(credential.get("host") or "").strip().lower()
        if not account:
            return None, "the account's username is required: it goes in the remote address"
        if not token:
            return None, "paste the token the account signs in with"
        if not host:
            return None, "name the host this token may be sent to, for example github.com"
        if len(host) > _MAX_HOST:
            return None, (
                f"that host is longer than {_MAX_HOST} characters, which is more "
                f"than an account record holds. Name the host on its own, like "
                f"github.com, rather than a full web address."
            )
        if len(account) > _MAX_ACCOUNT:
            return None, (
                f"that username is longer than {_MAX_ACCOUNT} characters, which "
                f"is more than an account record holds."
            )
        credential = {"account": account, "token": token, "host": host}

    return {
        "scope": scope.strip() if isinstance(scope, str) else None,
        "name": name,
        "email": email,
        "credential": credential,
    }, None


def _credential_shape(fields: dict[str, Any]) -> dict[str, Any]:
    """The record a git credential is stored as, without its value.

    The host IS the service. Turning "github.com" into "GitHub" would be the
    first line of per-service code in a framework whose whole claim is that it
    has none. `url` is where git spends a value, and `name` is the username
    that goes before it in the address.
    """
    credential = fields["credential"]
    return {
        "service": credential["host"],
        "account": credential["account"],
        "purpose": f"Pushes and pulls as {credential['account']} on {credential['host']}.",
        "injection": Injection(kind="url", name=credential["account"], prefix=""),
        "allowed_hosts": [credential["host"]],
    }


def _claim_credential_row(fields: dict[str, Any], previous_ref: str | None) -> tuple[str, bool]:
    """`(id, this call created it)` for the row the value will go into.

    NO VALUE IS WRITTEN HERE. Claiming the row and filling it in are separate
    steps because the fill is the only destructive one: a reconnect writes
    over the token every identity sharing that row depends on, and a request
    that then fails to record anything would have destroyed a working
    credential while reporting that nothing happened.

    Reuses the row this scope already pointed at rather than minting a second
    one per reconnect, which would leave the store filling with dead accounts
    the operator has to recognise and clear by hand. A row that was forgotten
    in the Credentials panel while this scope still named it is a fresh start,
    not a refusal.
    """
    from tesseract.credentials.store import CredentialStore

    store = CredentialStore()
    if previous_ref and any(row["id"] == previous_ref for row in store.list_public()):
        return previous_ref, False
    return str(store.create(CredentialDraft(**_credential_shape(fields)))["id"]), True


def _fill_credential_row(fields: dict[str, Any], ref: str) -> None:
    """Write the value into a row already claimed. The last thing that happens.

    Everything before this is undoable, so this is where a connect stops being
    reversible, and by then the record naming the row is already written. A
    failure here leaves the account exactly as it was: an old token still
    working, or a new row still showing "no value stored", both of which the
    panel says out loud.
    """
    from tesseract.credentials.models import PRIMARY_FIELD, Credential
    from tesseract.credentials.store import CredentialStore

    CredentialStore().put(
        Credential(id=ref, **_credential_shape(fields)),
        values={PRIMARY_FIELD: fields["credential"]["token"]},
    )


#: Held across a whole connect or disconnect.
#:
#: The two stores this touches have a lock each, and neither knows about the
#: other. Between reading which credential a scope names and writing the
#: record that names it, a second request can run the same read: two connects
#: submitted together both saw "nothing here yet", both made a row, and the
#: one whose record lost the race left its token in the store with nothing
#: pointing at it. One operator with two tabs open is the whole threat, and
#: one lock is the whole fix.
_IDENTITY_LOCK: asyncio.Lock | None = None


def _identity_lock() -> asyncio.Lock:
    """The lock, made on the loop that is about to use it.

    An `asyncio.Lock` built at import binds to whichever loop first has to
    WAIT on it, and raises on a contended acquire from any other loop. The
    backend has one loop for its lifetime; the test suite has one per test,
    where that would surface as a loop-binding error in place of whatever the
    test was actually asserting.
    """
    global _IDENTITY_LOCK
    loop = asyncio.get_running_loop()
    if _IDENTITY_LOCK is None or getattr(_IDENTITY_LOCK, "_loop", loop) is not loop:
        _IDENTITY_LOCK = asyncio.Lock()
    return _IDENTITY_LOCK


def _forget_credential(ref: str) -> None:
    """Remove one credential, reporting rather than raising.

    No reference check: the callers that reach this have already established
    that nothing names the row, and each of them is finishing a request whose
    failure is already being reported. A cleanup that raised would replace the
    operator's real error with a second one.
    """
    from tesseract.credentials.store import (
        CredentialStore,
        CredentialStoreError,
        UnknownCredentialError,
    )

    try:
        CredentialStore().remove(ref)
    except (UnknownCredentialError, CredentialStoreError) as exc:
        log.info("git identity: %s was left in the store (%s)", ref, exc)


def _drop_orphan_credential(ref: str | None) -> None:
    """Forget a credential no identity record names any more.

    Checked against the registry rather than assumed, because a machine
    identity and a project override can point at the same row: disconnecting
    one of them must not take the other's token with it.
    """
    from tesseract.orchestrator.projects.models import AMBIENT
    from tesseract.orchestrator.projects.store import ProjectStore, ProjectStoreError

    if not ref or ref == AMBIENT:
        return
    try:
        if ref in ProjectStore().credential_refs():
            return
    except ProjectStoreError as exc:
        log.info("git identity: %s was left in the store (%s)", ref, exc)
        return
    _forget_credential(ref)


async def _write_repo_identity(root: str, identity: Any | None, previous: Any) -> None:
    """Set or unset this repository's own `user.name` and `user.email`.

    **Local, never `--global`.** The operator's own name and their own sign-in
    are untouched by everything here, which is the property that lets an
    assistant identity exist on a machine a person also uses.

    The assistant does not depend on this: `git_tool` passes the identity on
    every commit it makes. This is for the operator's own terminal in the same
    folder, so a hand-made commit does not come out under a name that
    contradicts the one the panel is showing.

    Disconnect unsets only what connect wrote, checked by value. A local name
    the operator set themselves is theirs, and clearing it because it happened
    to be sitting where this looked would be this feature reaching outside its
    own record.
    """
    async def _set(key: str, value: str) -> None:
        code, out = await probes.probe(["git", "-C", root, "config", "--local", key, value])
        if code != 0:
            # A warning, not an info. This failed silently in a live connect
            # and the repository kept a name with no email beside it, which
            # reads as connected and commits as someone else the moment the
            # operator makes one by hand in that folder.
            log.warning("git identity: could not set %s in %s (%s)", key, root, out)

    async def _unset_if(key: str, expected: str) -> None:
        code, out = await probes.probe(["git", "-C", root, "config", "--local", "--get", key])
        if code != 0 or out.strip() != expected:
            return
        await probes.probe(["git", "-C", root, "config", "--local", "--unset", key])

    # SEQUENTIAL, and this is the one place in this file where that is the
    # point rather than an omission. Both keys are writes to the same
    # `.git/config`, and git takes `config.lock` for each: run together they
    # race, one loses, and the message is
    # `could not lock config file .git/config: File exists`. It happened on a
    # live connect and left the repository with a name and no email. The
    # project's parallel-by-default rule is about work that does not depend on
    # the previous step; two writers of one file are not that.
    if identity is not None:
        await _set("user.name", identity.name)
        await _set("user.email", identity.email)
    elif previous is not None:
        await _unset_if("user.name", previous.name)
        await _unset_if("user.email", previous.email)


async def connect_git_identity(request: web.Request) -> web.Response:
    """POST /api/settings/git/identity — say who the assistant pushes as.

    Body: ``{"scope": null|"proj-x", "name", "email", "credential": null |
    {"account", "token", "host"}}``. ``scope`` null is the machine-wide
    default; a project id overrides it for that project alone.

    Localhost-gated like every writer here, and this one carries a token in
    its body, so the gate is the difference between a local form post and
    anything on the network being able to hand this machine a credential.

    **The token is written to the credential store and to nowhere else.** It
    is not put in a remote URL on disk, not passed to `gh auth login` — which
    would replace the operator's own sign-in with the assistant's — and not
    returned in the response.
    """
    from tesseract.orchestrator.projects.store import ProjectStore

    if not is_localhost_request(request):
        return web.json_response({"error": "localhost only"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    fields, refusal = _identity_body(body)
    if fields is None:
        return web.json_response({"error": refusal}, status=400)

    store = ProjectStore()
    scope = fields["scope"]
    async with _identity_lock():
        return await _connect(store, scope, fields)


async def _connect(store: Any, scope: str | None, fields: dict[str, Any]) -> web.Response:
    """The body of a connect, with the lock already held."""
    from tesseract.credentials.store import CredentialStoreError
    from tesseract.orchestrator.projects.models import AMBIENT, GitIdentity
    from tesseract.orchestrator.projects.store import (
        ProjectStoreError,
        UnknownProjectError,
    )

    try:
        projects, _active, machine_identity = await asyncio.to_thread(store.snapshot)
    except ProjectStoreError as exc:
        return web.json_response({"error": str(exc)}, status=500)

    project = next((p for p in projects if p.id == scope), None) if scope else None
    if scope and project is None:
        return web.json_response({"error": f"no project {scope!r}"}, status=404)
    current = project.vcs.identity if project is not None else machine_identity
    previous_ref = (
        current.credential_ref
        if current is not None and current.credential_ref != AMBIENT
        else None
    )

    # Built BEFORE anything is stored, so the model's own bounds on a name and
    # an email are enforced while there is still nothing to undo. `AMBIENT` is
    # a placeholder here and the real ref is copied in below; validating with
    # one and writing the other is safe because the ref is minted by the store,
    # never by the request.
    try:
        identity = GitIdentity(
            name=fields["name"], email=fields["email"], credential_ref=AMBIENT
        )
    except ValidationError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    try:
        claimed = (
            await asyncio.to_thread(_claim_credential_row, fields, previous_ref)
            if fields["credential"]
            else (AMBIENT, False)
        )
    except (CredentialStoreError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:  # noqa: BLE001 — DPAPI refusing is the operator's news
        return web.json_response({"error": str(exc)}, status=500)
    ref, minted = claimed

    identity = identity.model_copy(update={"credential_ref": ref})
    try:
        await asyncio.to_thread(store.set_identity, identity, project_id=scope)
    except (UnknownProjectError, ProjectStoreError) as exc:
        # Nothing now names the row this call claimed. A row it MINTED holds
        # no value yet and is removed, because an account the operator never
        # added showing up in the Credentials panel is the confusing half of
        # a failure that already reported itself. A row it reused is left
        # exactly as it was: it still holds the token the unchanged record
        # depends on, and this request has not touched it.
        if minted:
            await asyncio.to_thread(_forget_credential, ref)
        if isinstance(exc, UnknownProjectError):
            return web.json_response({"error": f"no project {scope!r}"}, status=404)
        return web.json_response({"error": str(exc)}, status=500)

    if fields["credential"]:
        try:
            await asyncio.to_thread(_fill_credential_row, fields, ref)
        except Exception as exc:  # noqa: BLE001 — DPAPI refusing is the operator's news
            # The record is written and the value is not. Said plainly,
            # because the two halves are now in different states and the
            # panel will show exactly that: the account is named and has no
            # value, or still has the one it had.
            log.info("git identity: the value for %s was not stored (%s)", ref, exc)
            return web.json_response(
                {
                    "error": (
                        f"{fields['name']} is recorded as who this commits as, "
                        f"and the token could not be saved: {exc}. Nothing "
                        f"else changed. Try the token again in Settings, Git."
                    )
                },
                status=500,
            )

    # Only after the record is written: until then the old ref is still the
    # live one, and dropping it first would leave a window where the panel
    # says connected and the token has gone.
    if previous_ref and previous_ref != ref:
        await asyncio.to_thread(_drop_orphan_credential, previous_ref)
    if project is not None:
        await _write_repo_identity(str(project.root), identity, current)

    probes.invalidate_machine_probes()
    log.info(
        "git identity: %s now commits as %s (%s)",
        scope or "this machine",
        identity.name,
        "its own token" if ref != AMBIENT else "this machine's own credentials",
    )
    return web.json_response({"identity": identity_view(identity, {ref: True})})


async def disconnect_git_identity(request: web.Request) -> web.Response:
    """POST /api/settings/git/identity/remove — go back to ambient git.

    Body: ``{"scope": null|"proj-x"}``. Clears the record and forgets the
    token it named. The repository, its remote and its history are untouched,
    and a push by hand from a terminal works exactly as it did before anything
    was connected.
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
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be an object"}, status=400)

    scope = body.get("scope")
    if scope is not None and (not isinstance(scope, str) or not scope.strip()):
        return web.json_response(
            {"error": "scope must be a project id, or null for this machine"},
            status=400,
        )
    scope = scope.strip() if isinstance(scope, str) else None

    async with _identity_lock():
        return await _disconnect(scope)


async def _disconnect(scope: str | None) -> web.Response:
    """The body of a disconnect, with the lock already held."""
    from tesseract.orchestrator.projects.store import (
        ProjectStore,
        ProjectStoreError,
        UnknownProjectError,
    )

    store = ProjectStore()
    try:
        project = await asyncio.to_thread(store.get, scope) if scope else None
        if scope and project is None:
            return web.json_response({"error": f"no project {scope!r}"}, status=404)
        previous = await asyncio.to_thread(store.clear_identity, project_id=scope)
    except UnknownProjectError:
        return web.json_response({"error": f"no project {scope!r}"}, status=404)
    except ProjectStoreError as exc:
        return web.json_response({"error": str(exc)}, status=500)

    if previous is not None:
        await asyncio.to_thread(
            _drop_orphan_credential, previous.credential_ref
        )
        if project is not None:
            await _write_repo_identity(str(project.root), None, previous)

    probes.invalidate_machine_probes()
    log.info("git identity: %s is back to this machine's own git", scope or "this machine")
    return web.json_response({"disconnected": scope})
