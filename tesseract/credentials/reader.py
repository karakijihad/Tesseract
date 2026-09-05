"""Handing an account's fields to code that knows the service.

``spend.py`` puts ONE value in ONE place in a request the runtime builds. This
is the other half, and it exists because a sign-in is often not one value in
one place: an id beside a secret, a long-lived token exchanged for a
short-lived one, a signature computed over both. Describing those in a record
would be the runtime learning what a sign-in is, one vocabulary at a time.

So it does not. It reads every field an account holds and hands them to a
command as environment variables, which is how every other system on this
machine gives a program a secret. The code on the other side is the
assistant's own, it knows the service, and it mints whatever short-lived thing
that service wants at the moment it wants it. Nothing short-lived is stored,
pasted, or seen by the operator, which is why an account is set up once.

**The names are the record's, not the script's.** A field arrives under the
name it was asked for, uppercased, and nothing derives a name from a prefix or
a scheme. The assistant chose those names when it asked for the fields, so it
already knows them, and ``credential_list`` prints them so nothing has to
guess.

**Two callers, not one.** ``command_run`` is the shipped tool around this and
the one the operator approves by name, but a tool the assistant wrote into
``<home>/tools`` is executed inside this process (``kernel/home_tools.py``),
so it can import this function and skip the subprocess entirely. That route is
legitimate and is the same code. It matters when reasoning about who can read
an account: gating ``command_run`` is not gating the kind, and a search for
callers has to be a search for ``environment_for``.

**What this gives up, stated plainly, and it belongs to the KIND rather than
to one tool.** ``allowed_hosts`` stops controlling the destination. The runtime
builds no request here, so it checks no address; whatever holds these fields
makes its own calls and can send them anywhere. In-process that reach is wider
than a command's, with no timeout and no output ceiling around it. That is the
trade a ``.env`` file makes everywhere, and it is defensible for three reasons
that have to stay true: these are the assistant's OWN accounts, so the blast
radius is its own login rather than the operator's; ``SECURITY.md`` already
concedes that in unattended mode the runtime cannot protect a local credential
from code running as the same OS user; and every field of every account is
screened out of every tool result, so code that prints one yields
``[redacted: cred-x.client_secret]`` rather than the value. A tool the
assistant wrote is ASK unless the operator says otherwise, which is the
approval that stays in front of either route.

**Nothing adapts.** An account recorded as a header is refused here and told
to use ``api_request``; an account recorded for the environment is refused
there and told to use this. Guessing that one might mean the other is how a
value ends up somewhere the operator never approved.
"""

from __future__ import annotations

import os

from .models import InjectionKind
from .store import CredentialStore

#: The one kind this reads. Every other kind names a single place in a request
#: and belongs to ``spend.py``.
READABLE_KIND: InjectionKind = "env"

#: What a caller is told to use instead, when the record says the value goes
#: into a request rather than into an environment.
_SPEND_TOOL = "api_request"


class NotReadableAsEnvironment(RuntimeError):
    """The record says the value goes into a request, not into a command."""


class VariableAlreadySet(RuntimeError):
    """A field would replace a variable this computer already defines."""


def variable_name(field_name: str) -> str:
    """The environment variable one field arrives under.

    Uppercased and nothing else. A field name is already constrained to
    lowercase letters, digits and underscores (``normalise_field_name``), so
    this is a change of case rather than a translation, and the name the
    assistant asked for is the name it reads.
    """
    return field_name.upper()


def environment_for(
    credential_id: str, *, store: CredentialStore | None = None
) -> dict[str, str]:
    """``{VARIABLE: value}`` for every field ``credential_id`` holds.

    Raises :class:`NotReadableAsEnvironment` for a record that names a place
    in a request, and whatever the store raises for an unknown account, one
    with an empty field, or one sealed by another Windows account.

    The caller is runtime code at the point an account is about to be used,
    which is ``command_run`` launching a command or a home tool doing the work
    itself. Either way it is expected to use this and drop it: nothing here is
    written to disk, nothing is exported to the parent process, and the
    mapping goes out of scope with the call.
    """
    active = store or CredentialStore()
    injection = active.injection_of(credential_id)
    if injection.kind != READABLE_KIND:
        where = {
            "header": f"in the {injection.name} header",
            "query": f"in the {injection.name} query parameter",
            "url": "in the web address itself",
        }.get(injection.kind, f"as {injection.kind}")
        raise NotReadableAsEnvironment(
            f"{credential_id} is recorded as being sent {where}, which is a "
            f"request the runtime builds and checks the address of. It is not "
            f"handed to a command, so nothing ran. {_SPEND_TOOL} is what "
            f"sends it, and credential_setup is where the record is corrected "
            f"if that is wrong."
        )
    # Checked from the record, before anything is opened. A field that would
    # REPLACE a variable already in this process's environment is refused
    # rather than allowed to win: the names are proposed by the assistant and
    # the values typed in by the operator, and `path`, `pythonpath`, `comspec`
    # and `systemroot` are all valid field names. Letting one through would
    # let the contents of a box decide which program runs and what it loads,
    # which is not what an account is for.
    taken = [
        variable_name(name)
        for name in active.field_names(credential_id)
        if variable_name(name) in {key.upper() for key in os.environ}
    ]
    if taken:
        raise VariableAlreadySet(
            f"{credential_id} has "
            f"{'a field' if len(taken) == 1 else 'fields'} named "
            f"{', '.join(taken)}, which "
            f"{'is' if len(taken) == 1 else 'are'} already set in this "
            f"computer's environment. Giving a command the account's version "
            f"would change how it runs rather than how it signs in, so "
            f"nothing was read. Ask for the field under another name with "
            f"credential_request."
        )

    _, values = active.read_fields(credential_id)
    return {variable_name(name): value for name, value in values.items()}


__all__ = [
    "READABLE_KIND",
    "NotReadableAsEnvironment",
    "VariableAlreadySet",
    "environment_for",
    "variable_name",
]
