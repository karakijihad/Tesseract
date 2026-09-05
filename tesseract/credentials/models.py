"""Persisted shapes for the assistant's credential store.

A ``KEY=value`` line cannot say who the account belongs to, what it is for, or
which hosts it may be sent to, and every one of those is needed before a
consumer can spend a credential without a per-service branch in the runtime.
That is the whole reason this is not a second dotenv.

``extra="forbid"`` throughout, for the reason the project registry gives: a
typo'd key that parses into nothing would silently drop the host allowlist,
and a credential with no allowlist is one that goes anywhere.

**An account holds NAMED FIELDS, not a value.** One sealed string per field.
Most accounts hold exactly one, called ``value``, and that is the shape every
existing record migrates into, so nothing anywhere branches on which shape a
record is. The reason the plural exists is that a sign-in is often a set:
an id beside a secret, a key beside the account it belongs to. The assistant
knows what its service asks for and can say so; the operator is the one who
types the answers in.

The values themselves are base64 DPAPI blobs and are the ONE thing that never
leaves this module's own layer. :meth:`Credential.public` is what every caller
above the store sees, and it has no value field at all — not an empty one, not
a masked one. A shape that can carry a secret is a shape a later change can
accidentally fill in.
"""

from __future__ import annotations

import hashlib

import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")

# Where a consumer puts the value when it spends one. Closed on purpose, and
# each entry is a real injection SITE rather than a service: three for API
# calls, and `url` for a credential that belongs in a URL's userinfo, which is
# how git over HTTPS is authenticated and which none of the other three can
# describe. The case that is still deliberately absent is typing a password
# into a browser form, named as a known limit in the threat model rather than
# half-supported here.
#
# With `url`, `name` is the username that goes before the colon
# (`https://<name>:<value>@host`), so a consumer needs nothing but the record.
InjectionKind = Literal["header", "query", "env", "url"]

#: The field an account has when nobody has asked for another one. Named
#: rather than implied: it is what a legacy single-value record migrates into,
#: what a new account gets from the panel's one value box, and what
#: :class:`Injection` reaches for unless a record says otherwise.
PRIMARY_FIELD = "value"

#: What the panel writes above that one box. Set where the field is minted
#: rather than filled in by whichever surface renders it, so the account the
#: operator added last year and the one the assistant asked for this morning
#: say the same word.
PRIMARY_LABEL = "Value"

# A field name is an identifier the assistant proposes and the panel renders
# beside a box. Constrained because it ends up in a redaction marker and in a
# JSON key, and because a name that differs from another only by case or by a
# space is a name the operator cannot tell apart in a form.
_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

#: The longest one value may be. A bound rather than a preference: both
#: consumers read what a service or a command produced up to a ceiling and
#: check the WHOLE of what they read for stored values, so a value longer than
#: that ceiling could be cut in half before an exact match could find it, and
#: the leading half would be reported. Set far above any real key, token or
#: certificate chain and far below the ceilings in `runtime.yaml`, which is
#: what keeps the two facts from meeting.
MAX_VALUE_CHARS = 64_000

#: How many fields one account may hold. The operator fills these in by hand,
#: one box each, so the bound is on what a form can honestly ask for. The same
#: reasoning as `MAX_OPEN_REQUESTS`, applied one level down.
MAX_FIELDS = 12


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def mint_credential_id(service: str, taken: set[str] | None = None) -> str:
    """``cred-<slug>``, suffixed until it is free of ``taken``."""
    slug = _SLUG_STRIP.sub("-", service.strip().lower()).strip("-") or "account"
    candidate = f"cred-{slug}"
    existing = taken or set()
    if candidate not in existing:
        return candidate
    n = 2
    while f"{candidate}-{n}" in existing:
        n += 1
    return f"{candidate}-{n}"


def secret_label(credential_id: str, field_name: str) -> str:
    """What a removed value is called where a person reads it.

    The primary field is named by the ACCOUNT alone, and every other field
    carries its name after a dot. Naming the field is what makes four of them
    legible in a transcript; writing ``cred-x.value`` in every install that
    never asked for a second field would be noise in front of the one thing
    the marker exists to say.
    """
    if field_name == PRIMARY_FIELD:
        return credential_id
    return f"{credential_id}.{field_name}"


def normalise_field_name(name: str) -> str:
    """A proposed field name, folded, or raise ``ValueError``.

    Folding rather than refusing on case and spaces: the assistant proposes
    these from a service's documentation, where ``Client Secret`` and
    ``client_secret`` are the same thing, and a refusal there would be a
    failed call over a spelling.
    """
    folded = _SLUG_STRIP.sub("_", name.strip().lower()).strip("_")
    if not _FIELD_NAME.match(folded):
        raise ValueError(
            f"{name!r} is not a usable field name. Use letters, digits and "
            f"underscores, starting with a letter, for example 'client_secret'."
        )
    return folded


def _fold_hosts(hosts: list[str]) -> list[str]:
    """Lowercased, stripped, de-duplicated, order kept.

    Case-folded because DNS is: a check comparing ``API.Cloudflare.com``
    against the stored spelling would refuse the host the operator typed.
    """
    seen: list[str] = []
    for host in hosts:
        folded = host.strip().lower()
        if folded and folded not in seen:
            seen.append(folded)
    return seen


class Injection(BaseModel):
    """How a consumer spends this credential, and nothing about which one."""

    model_config = ConfigDict(extra="forbid")

    kind: InjectionKind
    # The header name, the query parameter name, or the username that goes
    # before the value in a url. Empty for `env`, and that is not an omission:
    # a record read into an environment hands over EVERY field, each under its
    # own name, so there is no one place for a single name to point at. See
    # `credentials/reader.py`.
    name: str = Field(default="", max_length=128)
    # What goes in front of the value, for the services that want one
    # (`Bearer `, `token `). Kept as data rather than inferred from `kind`,
    # because "Authorization" is used both ways by real APIs.
    prefix: str = Field(default="", max_length=32)
    # WHICH of the account's fields is placed. Defaulted rather than required,
    # so every record written before fields existed means what it always did.
    #
    # Folded by the same function that folds a field's own name, because a
    # membership test between a folded key and an unfolded one is the wrong
    # version of the check that guards this (`store.py::_merge_fields`). The
    # route builds this straight from a JSON body, so the constraint cannot
    # live in the callers.
    field: str = Field(default=PRIMARY_FIELD, max_length=40)

    @field_validator("field")
    @classmethod
    def _fold_field(cls, name: str) -> str:
        # Empty passes through for the after-validator to judge against the
        # kind. Folding it here would raise before that check could run, and
        # `env` has no field that is spent.
        return normalise_field_name(name) if name.strip() else ""

    @model_validator(mode="after")
    def _points_at_something_or_at_nothing(self) -> "Injection":
        """Every kind but ``env`` names one place and one value, and must.

        The check has to see ``kind``, so it cannot be a constraint on the
        field. Dropping ``min_length`` without this would let a header account
        be recorded with no header, which fails at the send rather than where
        it was written down.

        ``env`` is cleared rather than left alone. Both tools that build one of
        these default the name to ``Authorization``, so a caller that chose
        ``env`` and left the rest alone would store a header name on a record
        that has no header, and every surface rendering it would say something
        untrue about where the account goes.
        """
        if self.kind == "env":
            # BOTH, and for one reason. This kind hands over every field under
            # its own name, so there is neither a place to point at nor a
            # field that is "the one that is sent". `field` was the harder of
            # the two to see: leaving it defaulted to `value` made an account
            # asking for `client_id` and `client_secret` impossible to create
            # at all, because the store refuses a record naming a field it
            # does not hold, and that is the only account this kind exists for.
            if self.name:
                object.__setattr__(self, "name", "")
            if self.field:
                object.__setattr__(self, "field", "")
            return self
        if not self.field.strip():
            raise ValueError(
                f"a credential sent in a {self.kind} has to say which of the "
                f"account's values is the one that goes there."
            )
        if not self.name.strip():
            raise ValueError(
                f"a credential sent in a {self.kind} has to say which one. "
                f"Only an account read into a command's environment leaves "
                f"this empty, because it hands over every field by name."
            )
        return self


class CredentialField(BaseModel):
    """One named value on an account, sealed on its own.

    Sealed per field rather than as one blob, because the operator fills these
    in one at a time and over days: an account can hold two of the four things
    it needs and be honest about which two.
    """

    model_config = ConfigDict(extra="forbid")

    # What the panel puts above the box. Empty means the panel falls back to
    # the field's own name, which is legible enough for `client_secret` and
    # is what a record migrated from the single-value shape carries.
    label: str = Field(default="", max_length=120)
    # `repr=False` for the reason `Credential.secret` carried it: a traceback
    # frame or a ValidationError echoing the model does not go through
    # `public()`, and the log tree is one of the four sinks.
    secret: str | None = Field(default=None, repr=False)
    # Proof that `secret` was sealed by the Windows account now running. See
    # `seal_stamp_for`; per field, because the fields are sealed per field.
    seal_stamp: str | None = Field(default=None, repr=False)

    def state(self) -> str:
        """Whether this one field can be spent, in one word."""
        if self.secret is None:
            return "empty"
        if self.seal_stamp is None:
            return "unverified"
        expected = seal_stamp_for(self.secret)
        if expected is None:
            return "unverified"
        return "ready" if expected == self.seal_stamp else "foreign"

    def public(self, name: str) -> dict[str, Any]:
        return {
            "name": name,
            "label": self.label or name,
            "has_value": self.secret is not None,
            "state": self.state(),
        }


def _fold_field_labels(fields: dict[str, str]) -> dict[str, str]:
    """``{name: label}`` with the names folded and the order kept."""
    folded: dict[str, str] = {}
    for name, label in fields.items():
        folded[normalise_field_name(name)] = label.strip()
    if len(folded) > MAX_FIELDS:
        raise ValueError(
            f"an account may hold {MAX_FIELDS} fields, and this asks for "
            f"{len(folded)}. Each one is a box a person fills in by hand."
        )
    return folded


class CredentialDraft(BaseModel):
    """What a caller can describe: an account, with no identity of its own.

    Deliberately has no ``id``, no values, no ``requested`` and no timestamps.
    Every one of those belongs to the store: the id because it must be minted
    inside the lock that writes it, the rest because a caller that could set
    them could contradict what the store is about to record. Two callers used
    to build a provisional id here and the store threw both away, which is the
    shape a draft removes rather than documents.
    """

    model_config = ConfigDict(extra="forbid")

    service: str = Field(min_length=1, max_length=120)
    account: str = Field(default="", max_length=200)
    purpose: str = Field(default="", max_length=500)
    injection: Injection
    allowed_hosts: list[str] = Field(default_factory=list)
    # ``{name: label}``. Empty means the one field every account has.
    fields: dict[str, str] = Field(default_factory=dict)

    _normalize_hosts = field_validator("allowed_hosts")(
        lambda cls, hosts: _fold_hosts(hosts)
    )
    _normalize_fields = field_validator("fields")(
        lambda cls, fields: _fold_field_labels(fields)
    )

    def field_labels(self) -> dict[str, str]:
        """What this draft asks the operator for, never empty."""
        return dict(self.fields) or {PRIMARY_FIELD: PRIMARY_LABEL}


def seal_stamp_for(sealed: str) -> str | None:
    """Proof that ``sealed`` was sealed by the account now running, or ``None``.

    A hash over the account and the sealed bytes together. Neither half alone
    would do: the account alone would still attest to a blob someone swapped
    into the file by hand, and the blob alone would say nothing about who can
    open it.

    Hashed rather than stored, because equality is the only question anything
    asks of it, and a raw SID is a machine identifier written into a file this
    project copies onto other machines.

    ``None`` when the account cannot be read at all, which is how a machine
    that cannot answer the question reports that it does not know rather than
    that the value is foreign.
    """
    from . import dpapi

    account = dpapi.account_id()
    if account is None:
        return None
    return hashlib.sha256(f"{account}|{sealed}".encode("utf-8")).hexdigest()


class Credential(BaseModel):
    """One account the assistant has, or has asked for.

    An account with no field holding a value stores nothing. Paired with
    ``requested=True`` that is an open request the operator has not answered;
    on its own it is an entry whose values were cleared.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    service: str = Field(min_length=1, max_length=120)
    # The account or username the credential belongs to. Never the operator's
    # own account by default — these are the assistant's own logins, and the
    # panel says so.
    account: str = Field(default="", max_length=200)
    # What the assistant may use it for, in the operator's words. This is the
    # half the model reads, so it is the half that has to be worth reading.
    purpose: str = Field(default="", max_length=500)
    injection: Injection
    # Hostnames this value may be sent to. Empty means none: a credential with
    # no allowlist is unusable rather than universal, because the failure of
    # the opposite default is silent and total.
    allowed_hosts: list[str] = Field(default_factory=list)
    # Every value this account holds, keyed by field name. Order is the order
    # the fields were asked for, which is the order the panel renders the
    # boxes in, so the operator fills them in the order the assistant
    # explained them.
    fields: dict[str, CredentialField] = Field(default_factory=dict)
    requested: bool = False
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)

    @model_validator(mode="before")
    @classmethod
    def _migrate_single_value(cls, data: Any) -> Any:
        """A record written before fields existed becomes a record with one.

        Runs before the ``extra="forbid"`` check, which is what lets the two
        retired keys be read and dropped rather than refused. The operator's
        own store predates this phase, and a migration that only ran on a
        fresh install would be a feature that broke the one install it had.

        Nothing is written back here. The store rewrites the whole file on its
        next save, so the old shape survives on disk until something changes
        it, which is what makes a revert to the previous code possible.
        """
        if not isinstance(data, dict):
            return data
        if "secret" not in data and "seal_stamp" not in data:
            return data
        migrated = dict(data)
        secret = migrated.pop("secret", None)
        stamp = migrated.pop("seal_stamp", None)
        if secret is None:
            stamp = None
        fields = dict(migrated.get("fields") or {})
        # `secret` may be null: a cleared account, or an open request nobody
        # has answered. The BOX still has to exist, because it is the only
        # thing the panel renders an input for, and a row that migrated to no
        # fields at all would be one the operator could never fill in.
        fields.setdefault(
            PRIMARY_FIELD,
            {"label": PRIMARY_LABEL, "secret": secret, "seal_stamp": stamp},
        )
        migrated["fields"] = fields
        return migrated

    @field_validator("allowed_hosts")
    @classmethod
    def _normalize_hosts(cls, hosts: list[str]) -> list[str]:
        return _fold_hosts(hosts)

    @field_validator("fields")
    @classmethod
    def _check_field_names(
        cls, fields: dict[str, CredentialField]
    ) -> dict[str, CredentialField]:
        # An account with no box is an account nothing can ever be typed into,
        # and the panel renders one input per field. Filled in here rather
        # than at each write, because there are three of those and a record
        # arriving from disk is a fourth.
        if not fields:
            return {PRIMARY_FIELD: CredentialField(label=PRIMARY_LABEL)}
        for name in fields:
            if not _FIELD_NAME.match(name):
                raise ValueError(
                    f"{name!r} is not a usable field name. Use letters, digits "
                    f"and underscores, starting with a letter."
                )
        if len(fields) > MAX_FIELDS:
            raise ValueError(
                f"an account may hold {MAX_FIELDS} fields, and this has "
                f"{len(fields)}."
            )
        return fields

    def has_value(self) -> bool:
        """Whether ANY field holds something.

        The literal reading of the name, on purpose. Every question about
        whether this account can be USED goes through :meth:`spend_state`,
        which is the distinction four readers got wrong when there was one
        value and `has_value` was the only thing to read.
        """
        return any(f.secret is not None for f in self.fields.values())

    def missing_fields(self) -> list[str]:
        """The names of the fields nobody has filled in yet, in order."""
        return [name for name, f in self.fields.items() if f.secret is None]

    def needs_operator(self) -> bool:
        """Whether there is a box on this row for a person to fill in.

        Separate from ``requested`` on purpose. ``requested`` is read as an
        unconditional override by :meth:`spend_state` and by the panel, both
        of which answer "waiting" before looking at a single field, so setting
        it when the assistant asks for one more field would make a row holding
        four good values report that nothing is known about it. This says the
        smaller, true thing: somebody has to type something here. It is what
        the panel sorts and counts on, so an ask for a field surfaces exactly
        as an ask for a whole account does.
        """
        return self.requested or bool(self.missing_fields())

    def spend_state(self) -> str:
        """Whether this row's values can be spent, in one word.

        Derived HERE so that every shape a caller can hold carries it. Two
        panels and two tools used to read `has_value` and call the row ready,
        and `has_value` only says a blob is present: a store copied from
        another machine, or from another Windows account on this one, carries
        blobs that never open, and every surface said ready until a push
        failed. Four readers inferring one thing from a field that does not
        mean it is four places for the same mistake.

        With several fields the answer is the WORST of them, and the order
        below is the order of how much stands in the way. `foreign` outranks
        `incomplete` because the two have different remedies and only one of
        them is "type the rest in": a value sealed elsewhere has to be cleared
        and added again, and a row that reported the easier problem first
        would send the operator round twice.

        `unverified` is not a hedge. It is a field whose stamp is missing,
        which is one written before the stamp existed or one sealed where the
        account could not be read, and the honest answer for both is that this
        has not been checked rather than a guess in either direction. The
        first spend that succeeds stamps it and it never says this again.
        """
        if self.requested:
            return "waiting"
        if not self.fields or not self.has_value():
            return "empty"
        states = {f.state() for f in self.fields.values()}
        if "foreign" in states:
            return "foreign"
        if "empty" in states:
            return "incomplete"
        if "unverified" in states:
            return "unverified"
        return "ready"

    def public(self) -> dict[str, Any]:
        """Everything about this credential except what it is.

        The return is built field by field rather than dumped-and-popped. A
        pop is one rename away from putting a secret back on the wire, and
        nothing downstream would notice until it was in a transcript.
        """
        return {
            "id": self.id,
            "service": self.service,
            "account": self.account,
            "purpose": self.purpose,
            "injection": self.injection.model_dump(mode="json"),
            "allowed_hosts": list(self.allowed_hosts),
            "fields": [
                field.public(name) for name, field in self.fields.items()
            ],
            "has_value": self.has_value(),
            "spend_state": self.spend_state(),
            "needs_operator": self.needs_operator(),
            "requested": self.requested,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


#: What each :meth:`Credential.spend_state` answer means, in one fragment a
#: caller wraps its own sentence around. Three tools had a dict of these each,
#: and the three had already drifted to three different phrasings of
#: ``unverified``. The state is decided in one place, so the words for it are
#: too.
#:
#: Each fragment has to read after "it is" AND on its own at the end of a
#: list line, because those are the two shapes the callers have. That is what
#: rules out the single word "ready", which is why the three drifted apart in
#: the first place.
SPEND_STATE_WORDS: dict[str, str] = {
    "waiting": "waiting on the operator",
    "empty": "waiting for a value",
    "incomplete": "missing some of its values",
    "unverified": "stored, but not checked yet on this computer",
    "foreign": (
        "sealed by another Windows account, so it cannot be sent. The "
        "operator adds it again in Settings, Credentials"
    ),
    "ready": "ready to use",
}


class CredentialFile(BaseModel):
    """The whole store, as it sits on disk."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    credentials: dict[str, Credential] = Field(default_factory=dict)


__all__ = [
    "MAX_FIELDS",
    "MAX_VALUE_CHARS",
    "SPEND_STATE_WORDS",
    "PRIMARY_FIELD",
    "PRIMARY_LABEL",
    "Credential",
    "CredentialDraft",
    "CredentialField",
    "CredentialFile",
    "Injection",
    "InjectionKind",
    "mint_credential_id",
    "normalise_field_name",
    "secret_label",
    "utc_now_iso",
]
