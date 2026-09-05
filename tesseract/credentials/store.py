"""Read/write of ``<TESSERACT_HOME>/credentials/credentials.json``.

Two layers, and the boundary between them is the point of the file:

* everything a caller above this module can reach returns
  :meth:`Credential.public` shapes, which have no value field;
* :meth:`CredentialStore.resolve` is the one method a CONSUMER decrypts
  through, called at the moment of the request, and no tool is allowed to
  reach it. GOVERNANCE §1 is what that split implements.

:meth:`CredentialStore.iter_secrets` is the second and last method that
decrypts, and it is not a consumer path. ``credentials/redaction.py`` is its
only caller: finding a stored value inside a string is the one job that needs
every value at once, and it is the job of keeping those values OUT of the
record rather than spending them. It is named on the class so that "what
decrypts here" is two documented methods rather than one documented method and
a module reaching through ``_load`` and ``_open`` from outside.

A corrupt or unreadable store raises rather than resolving to an empty one,
for the reason the project registry gives: a silent empty store would let a
write clobber every credential the operator had, and the panel would report
"no accounts" for a file that was merely malformed.
"""

from __future__ import annotations

import base64
import binascii
import logging
import threading
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from tesseract.lib.yaml_io import atomic_write_text

from .dpapi import DpapiError, DpapiUnavailable, protect, unprotect
from .models import (
    MAX_FIELDS,
    MAX_VALUE_CHARS,
    Credential,
    CredentialDraft,
    CredentialField,
    CredentialFile,
    Injection,
    mint_credential_id,
    normalise_field_name,
    seal_stamp_for,
    secret_label,
    utc_now_iso,
)
from .paths import store_path

# What DPAPI records alongside the blob. Visible in a Windows credential audit,
# so it says which application put it there.
_DPAPI_DESCRIPTION = "TESSERACT assistant credential"

log = logging.getLogger(__name__)

# One JSON file under a read-modify-write cycle, serialized in-process. The
# same bound the project registry lives with, and for the same reason: this is
# single-operator, single-machine state written by panel clicks and tool calls,
# not a hot path.
_WRITE_LOCK = threading.Lock()

# How many accounts may sit waiting on the operator at once. The panel is
# where these are answered one at a time, so the bound is on the operator's
# attention rather than on disk: `credential_request` is AUTO, and an
# unbounded queue of asks is how the surface stops being answerable.
MAX_OPEN_REQUESTS = 8


class CredentialStoreError(RuntimeError):
    """The store exists but could not be read as a store."""


class UnknownCredentialError(KeyError):
    """No credential with that id."""

    def __str__(self) -> str:  # KeyError's repr quotes the message
        return self.args[0] if self.args else super().__str__()


class CredentialNotSet(RuntimeError):
    """The entry exists and holds no value yet."""


class TooManyOpenRequests(RuntimeError):
    """Too many accounts already wait on the operator to be worth another."""


class HostNotAllowed(RuntimeError):
    """The caller asked to spend a credential on a host it does not name."""


class UnknownCredentialField(KeyError):
    """The account has no field by that name."""

    def __str__(self) -> str:  # KeyError's repr quotes the message
        return self.args[0] if self.args else super().__str__()


class TooManyFields(RuntimeError):
    """The account would hold more boxes than a person can be asked to fill."""


class CredentialStore:
    """Store access. Resolves its path at call time unless one is injected.

    ``path=None`` is the production shape: every operation re-resolves
    ``TESSERACT_HOME``, so a test that redirects the env after constructing a
    store still gets the scratch tree.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else store_path()

    # ── disk ────────────────────────────────────────────────────────────

    def _load(self) -> CredentialFile:
        path = self.path
        if not path.exists():
            return CredentialFile()
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CredentialStoreError(
                f"credential store unreadable at {path}: {exc}"
            ) from exc
        try:
            return CredentialFile.model_validate_json(raw)
        except ValidationError as exc:
            raise CredentialStoreError(
                f"credential store at {path} is malformed: {exc}"
            ) from exc

    def _save(self, data: CredentialFile) -> None:
        atomic_write_text(
            self.path,
            data.model_dump_json(indent=2) + "\n",
            prefix=".credentials-",
        )
        # The redactor caches decrypted values and re-reads on a stat change.
        # A write that only swaps one value can land in the same filesystem
        # tick at the same length, which is byte-for-byte the same stat — the
        # defect the memory store already carries on record. Telling it
        # directly is what makes the stat a backstop for an outside edit
        # rather than the mechanism.
        from .redaction import invalidate

        invalidate()

    # ── reads that carry no value ───────────────────────────────────────

    def list_public(self) -> list[dict[str, Any]]:
        """Every entry, with no values in it.

        Rows with a box for a person to fill come first, then service order.
        The sort lives HERE rather than in the panel so every surface inherits
        it: an ask is the one thing in this store that is addressed to the
        operator, and a surface that has to remember to float it is a surface
        that will eventually forget.

        Two ranks, not one. An account the assistant explicitly ASKED for
        comes first, because that is a decision addressed to the operator;
        then anything else with an empty box, which is a job rather than a
        decision; then service order. An ask for one more field on an account
        that already holds a value leaves `requested` alone on purpose
        (`spend_state` reads that flag as an unconditional "nothing is known
        here"), so `needs_operator` is what floats it, and it floats to the
        second rank rather than displacing an unanswered account request.
        """
        data = self._load()
        entries = sorted(
            data.credentials.values(),
            key=lambda c: (
                not c.requested,
                not c.needs_operator(),
                c.service.lower(),
                c.id,
            ),
        )
        return [entry.public() for entry in entries]

    # ── writes ──────────────────────────────────────────────────────────

    def put(
        self, credential: Credential, *, values: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Update an EXISTING entry, keyed by id.

        Update-only on purpose. It used to add-or-replace, which made it a
        second way to put a row in the store, reachable without ever going
        through `_insert` and so without minting an id under the lock. One
        insert path is the property; a method that quietly creates when handed
        an unknown id is that property with an exception in it.

        **Fields are UNIONED, never replaced.** The caller describes an
        account, not the sealed blobs inside it: the route builds a
        `Credential` from a form and a tool builds one from its own input, and
        neither can carry a value it is not allowed to read. So an existing
        field is kept whatever the caller says, its label is refreshed only
        when the caller offers one, and a field the caller names that is not
        there yet is added empty. Dropping a field the caller omitted would
        make an operator correcting a purpose destroy the credential they were
        describing, which is the trap the Keys panel had to be pulled back
        from.

        ``values`` fills in the named fields and leaves every other one alone.
        ``None`` keeps everything. Clearing is :meth:`clear_value`, a separate
        call, for that same reason.
        """
        with _WRITE_LOCK:
            data = self._load()
            existing = self._require(data, credential.id)
            merged_fields = self._merge_fields(
                existing.fields,
                credential.fields,
                values or {},
                credential.injection.field,
            )
            requested = credential.requested
            if values and not any(f.secret is None for f in merged_fields.values()):
                # The ask is answered when nothing is left to type. A row the
                # operator half filled is still one they are being waited on
                # for, and clearing the flag on the first value would sink it
                # out of the panel's waiting list with boxes still empty.
                requested = False
            merged = credential.model_copy(
                update={
                    "fields": merged_fields,
                    "requested": requested,
                    "created_at": existing.created_at,
                    "updated_at": utc_now_iso(),
                }
            )
            data.credentials[merged.id] = merged
            self._save(data)
            return merged.public()

    def _merge_fields(
        self,
        existing: dict[str, CredentialField],
        declared: dict[str, CredentialField],
        values: dict[str, str],
        spend_field: str,
    ) -> dict[str, CredentialField]:
        """Existing fields, plus whatever the caller named, plus new values.

        Sealing happens here, under the caller's lock, and stamping happens
        with it. The stamp describes THIS blob, so a path that could update
        one without the other is a path that can attest to bytes that are no
        longer there.

        **This is where ``injection.field`` is checked against the fields that
        will exist**, and it is the only place. The rule used to live in
        `credential_setup`'s own `if`, which left every other writer free to
        break it: the panel route builds an `Injection` straight from a JSON
        body, and a record naming a field it does not hold reported itself
        `ready` right up until the spend that failed. `put` and `_insert` both
        funnel through here, and here is the first moment the merged set is
        known, so the check is made once rather than replicated per caller.
        """
        merged = dict(existing)
        for name, field in declared.items():
            kept = merged.get(name)
            if kept is None:
                merged[name] = CredentialField(label=field.label)
            elif field.label and field.label != kept.label:
                merged[name] = kept.model_copy(update={"label": field.label})
        for name, value in values.items():
            folded = normalise_field_name(name)
            if folded not in merged:
                raise UnknownCredentialField(
                    f"this account has no field called {folded!r}. Its fields "
                    f"are: {', '.join(merged) or 'none yet'}."
                )
            sealed = self._seal(value)
            merged[folded] = merged[folded].model_copy(
                update={"secret": sealed, "seal_stamp": seal_stamp_for(sealed)}
            )
        # Checked here rather than left to the model, because both callers
        # write through `model_copy`, which does not re-validate.
        if len(merged) > MAX_FIELDS:
            raise TooManyFields(
                f"that would leave the account holding {len(merged)} fields, "
                f"and {MAX_FIELDS} is the limit. Each one is a box a person "
                f"fills in by hand. Nothing was recorded."
            )
        # An empty spend field is not a missing one. `Injection` clears it
        # for the kind that hands every field to a command rather than sending
        # one of them, and a record that spends nothing has nothing to check.
        if spend_field and spend_field not in merged:
            raise UnknownCredentialField(
                f"the value to send is {spend_field!r}, which is not one of "
                f"this account's fields ({', '.join(merged) or 'none'}). The "
                f"account would say it was ready and fail the moment it was "
                f"used, so nothing was recorded. credential_request is where "
                f"another field is asked for."
            )
        return merged

    def _insert(
        self,
        data: CredentialFile,
        draft: CredentialDraft,
        *,
        values: dict[str, str] | None,
        requested: bool,
    ) -> Credential:
        """Mint an id against ``data`` and add the row. Caller holds the lock.

        Takes a DRAFT, which has no id, so there is nothing for a caller to
        mint and nothing for this to throw away. Both entry points used to
        build a provisional id that was discarded here, which read as though
        the caller's id meant something.

        Sealing the values happens under the lock too, and that is a decision
        rather than an oversight. The project registry carries the opposite
        one, refusing to stat a path inside its write lock because an unmounted
        drive would serialise every writer behind one hung syscall. A DPAPI
        call is not in that category: it is a bounded, local, in-process
        ctypes round trip that touches no filesystem and cannot block on a
        device, so holding the lock across it costs nothing worth reclaiming.
        """
        service = draft.service.strip()
        stamped = utc_now_iso()
        fields = self._merge_fields(
            {},
            {
                name: CredentialField(label=label)
                for name, label in draft.field_labels().items()
            },
            values or {},
            draft.injection.field,
        )
        entry = Credential(
            id=mint_credential_id(service, set(data.credentials)),
            service=service,
            account=draft.account.strip(),
            purpose=draft.purpose.strip(),
            injection=draft.injection,
            allowed_hosts=list(draft.allowed_hosts),
            fields=fields,
            requested=requested,
            created_at=stamped,
            updated_at=stamped,
        )
        data.credentials[entry.id] = entry
        return entry

    def create(
        self, draft: CredentialDraft, *, values: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Add a row the operator is entering themselves.

        The id is minted from the roster this method loaded inside the lock it
        is about to write under. A caller that minted its own and then wrote
        would leave a window in which two of them mint the same one and the
        second silently replaces the first, which is the defect on record
        against the project registry.
        """
        with _WRITE_LOCK:
            data = self._load()
            entry = self._insert(data, draft, values=values, requested=False)
            self._save(data)
            return entry.public()

    def request_fields(
        self, credential_id: str, fields: dict[str, str]
    ) -> dict[str, Any]:
        """Ask the operator for more values on an account that already exists.

        ``{name: label}``, added empty. This is the whole of what the
        assistant may do to an account it already has: name what the service
        asks it for, so the panel grows a box per field and the operator types
        the answers in.

        **It cannot widen anything.** No host, no injection, no purpose passes
        through here, which is what keeps this on the `auto` side of the line
        while `credential_setup` stays on the `ask` side. A field name is a
        description; a destination is not, and there is no destination in this
        signature to be one.

        A field already present keeps whatever it holds and, if it holds
        nothing, takes the new label: re-asking is how the assistant corrects
        a name it explained badly, and destroying a value the operator had
        already typed would make asking twice expensive.
        """
        with _WRITE_LOCK:
            data = self._load()
            entry = self._require(data, credential_id)
            wanted = {
                normalise_field_name(name): label.strip()
                for name, label in fields.items()
            }
            if not wanted:
                raise ValueError("name at least one field to ask for")
            merged = dict(entry.fields)
            for name, label in wanted.items():
                kept = merged.get(name)
                if kept is None:
                    merged[name] = CredentialField(label=label)
                elif kept.secret is None and label:
                    merged[name] = kept.model_copy(update={"label": label})
            if merged == entry.fields:
                # Nothing to say that has not already been said. Rewriting
                # would only bump `updated_at` and float a stale ask to the
                # top of a surface the operator reads by recency.
                return entry.public()
            if len(merged) > MAX_FIELDS:
                raise TooManyFields(
                    f"{credential_id} would hold {len(merged)} fields, and "
                    f"{MAX_FIELDS} is the limit. Each one is a box a person "
                    f"fills in by hand. Nothing was recorded."
                )
            updated = entry.model_copy(
                update={"fields": merged, "updated_at": utc_now_iso()}
            )
            data.credentials[credential_id] = updated
            self._save(data)
            return updated.public()

    def request(self, draft: CredentialDraft) -> tuple[dict[str, Any], bool]:
        """Record an ask for an account the assistant does not have.

        Returns the row and whether it was CREATED. The match, the cap, the id
        and the write all happen under one lock acquisition.

        **Which row matches is decided, not stumbled into.** The store supports
        several accounts per service on purpose (`mint_credential_id` suffixes,
        and a draft carries an `account`), so taking whichever one a dict
        iterated first meant the answer depended on insertion order: with a
        ready `cred-cloudflare` and a cleared `cred-cloudflare-2`, the same ask
        could report the account ready or reopen the empty one. The account
        narrows it when the caller supplied one, and a row holding a VALUE then
        outranks one that does not, so "you already have this" always wins over
        "I have reopened a request".

        **A reopened row takes the new ask's details.** It used to keep the old
        ones, which composed badly with the match above: the operator could be
        shown one account's purpose and host allowlist as the justification for
        a different account's request, and then fill in a value against an
        allowlist nobody had reviewed for it. The row holds no value at this
        point, so replacing its metadata costs nothing and is the only version
        the operator can act on honestly.
        """
        with _WRITE_LOCK:
            data = self._load()
            service = draft.service.strip()
            wanted = draft.account.strip().lower()

            matches = [
                c
                for c in data.credentials.values()
                if c.service.lower() == service.lower()
            ]
            if wanted:
                # A named account NARROWS, absolutely. Falling back to every
                # row for the service when none matched inverted this method's
                # own bug: asking for an account the store does not have
                # answered about a different one, reporting someone else's
                # credential as ready and creating nothing. An empty set is the
                # right answer and falls through to the create branch below.
                matches = [
                    c for c in matches if c.account.strip().lower() == wanted
                ]
            # A row holding ANYTHING outranks one holding nothing, so "you
            # already have this" always wins over "I have reopened a
            # request". `has_value` rather than a readiness check on
            # purpose: a half-filled account is one the operator is already
            # working on, and reopening it would replace their in-progress
            # labels with a fresh ask.
            matches.sort(key=lambda c: (not c.has_value(), c.id))
            existing = matches[0] if matches else None

            if existing is not None and existing.has_value():
                return existing.public(), False

            if existing is not None:
                reopened = existing.model_copy(
                    update={
                        "service": service,
                        "account": draft.account.strip(),
                        "purpose": draft.purpose.strip(),
                        "injection": draft.injection,
                        "allowed_hosts": list(draft.allowed_hosts),
                        "fields": {
                            name: CredentialField(label=label)
                            for name, label in draft.field_labels().items()
                        },
                        "requested": True,
                    }
                )
                # Nothing to say that has not already been said. Rewriting
                # here would only bump `updated_at` and make a stale ask look
                # freshly made on the surface the operator reads by recency.
                #
                # The stamp is applied AFTER this comparison on purpose, and
                # the order is load-bearing: pydantic equality covers every
                # field, so folding `updated_at` into the copy above would make
                # this always false and quietly restore the no-op write.
                if reopened == existing:
                    return existing.public(), False
                if not existing.requested:
                    self._refuse_if_queue_full(data)
                reopened = reopened.model_copy(update={"updated_at": utc_now_iso()})
                data.credentials[reopened.id] = reopened
                self._save(data)
                return reopened.public(), False

            self._refuse_if_queue_full(data)
            entry = self._insert(data, draft, values=None, requested=True)
            self._save(data)
            return entry.public(), True

    @staticmethod
    def _refuse_if_queue_full(data: CredentialFile) -> None:
        """Refuse when the operator already has enough decisions in front of
        them. Counted over rows, not over calls, so a reopen costs the same as
        a create: both put an answerable thing in the panel."""
        open_rows = sum(1 for c in data.credentials.values() if c.requested)
        if open_rows >= MAX_OPEN_REQUESTS:
            raise TooManyOpenRequests(
                f"{open_rows} accounts are already waiting on the operator, "
                f"which is the limit ({MAX_OPEN_REQUESTS}). The panel is where "
                f"they answer these, and a queue of them is how it stops being "
                f"answerable. Nothing was recorded."
            )

    def clear_value(self, credential_id: str) -> dict[str, Any]:
        """Drop every value and keep the entry, so the assistant still knows
        the account exists and can ask for it again.

        The FIELDS stay: they are what the assistant asked for, and forgetting
        the question along with the answer would mean asking again just to get
        the boxes back. Replacing one value is typing into its box, so there is
        no second way to empty one.
        """
        with _WRITE_LOCK:
            data = self._load()
            entry = self._require(data, credential_id)
            cleared = {
                name: field.model_copy(update={"secret": None, "seal_stamp": None})
                for name, field in entry.fields.items()
            }
            entry = entry.model_copy(
                update={"fields": cleared, "updated_at": utc_now_iso()}
            )
            data.credentials[credential_id] = entry
            self._save(data)
            return entry.public()

    def remove(self, credential_id: str) -> None:
        with _WRITE_LOCK:
            data = self._load()
            self._require(data, credential_id)
            data.credentials.pop(credential_id)
            self._save(data)

    # ── the one path that decrypts ──────────────────────────────────────

    def resolve(self, credential_id: str, host: str) -> tuple[Injection, str]:
        """The value, for a consumer about to send it to ``host``.

        Never reachable from a tool. The caller is runtime code at the request
        site, it receives the value and the shape of the injection together
        because sending one without the other is how a token ends up in a
        query string it was meant to be a header in, and it is expected to
        drop the value the moment the request is built.

        WHICH field is returned is the record's decision, not the caller's:
        ``injection.field`` names it, and an account holding four values still
        spends exactly the one it was set up to spend. A caller that could
        choose would be a caller that could send the wrong half of a sign-in
        to a host approved for the other half.

        The host check is exact and case-folded. No suffix matching: a stored
        ``cloudflare.com`` that also opened ``evil.cloudflare.com.attacker.io``
        is a well-known way to write this wrong, and an operator who wants a
        subdomain can name it.
        """
        entry = self._require(self._load(), credential_id)
        name = entry.injection.field
        field = entry.fields.get(name)
        if field is None or field.secret is None:
            others = [f for f in entry.missing_fields() if f != name]
            raise CredentialNotSet(
                f"{credential_id!r} has nothing stored for {name!r}. The "
                f"operator fills that in in Settings, Credentials"
                + (f", along with: {', '.join(others)}." if others else ".")
            )
        target = host.strip().lower()
        if target not in entry.allowed_hosts:
            allowed = ", ".join(entry.allowed_hosts) or "nothing yet"
            raise HostNotAllowed(
                f"{credential_id!r} may be sent to {allowed}, not {host!r}; "
                f"nothing was sent."
            )
        value = self._open(field.secret, secret_label(credential_id, name))
        if field.seal_stamp is None:
            # It opened, so it was sealed here, and now there is proof the
            # panel can read without opening it again. Only ever runs once
            # per field: the row written before this field existed.
            self._stamp_now(credential_id, name, field.secret)
        return entry.injection, value

    def _stamp_now(self, credential_id: str, field_name: str, sealed: str) -> None:
        """Record that ``sealed`` opens here. Never raises.

        This is called from a spend, and a spend that failed because a panel
        label could not be written would be this feature breaking the thing it
        exists to describe. The stamp is checked against the blob it names, so
        a value replaced between the read and this write is left alone rather
        than attested to.
        """
        stamp = seal_stamp_for(sealed)
        if stamp is None:
            return
        try:
            with _WRITE_LOCK:
                data = self._load()
                entry = data.credentials.get(credential_id)
                field = None if entry is None else entry.fields.get(field_name)
                if (
                    field is None
                    or field.secret != sealed
                    or field.seal_stamp is not None
                ):
                    return
                assert entry is not None
                data.credentials[credential_id] = entry.model_copy(
                    update={
                        "fields": {
                            **entry.fields,
                            field_name: field.model_copy(
                                update={"seal_stamp": stamp}
                            ),
                        }
                    }
                )
                self._save(data)
        except Exception as exc:  # noqa: BLE001 — a label is not worth a failed spend
            log.info("credentials: %s could not be stamped (%s)", credential_id, exc)

    def injection_of(self, credential_id: str) -> Injection:
        """How this account is spent, without opening anything.

        Separate from :meth:`read_fields` so a consumer can refuse a record
        that is not its kind BEFORE any value is decrypted. Reading four
        secrets into memory and then declining to use them is a worse version
        of the same answer.
        """
        return self._require(self._load(), credential_id).injection

    def field_names(self, credential_id: str) -> list[str]:
        """What this account's boxes are called, without opening any of them.

        Beside :meth:`injection_of` and for the same reason: a consumer that
        may refuse the call should be able to decide from the record rather
        than from the values.
        """
        return list(self._require(self._load(), credential_id).fields)

    def read_fields(self, credential_id: str) -> tuple[Injection, dict[str, str]]:
        """EVERY field this account holds, for code that does its own sign-in.

        The counterpart to :meth:`resolve`, and the two differ in exactly two
        ways, both of them deliberate and both of them the reason this is a
        separate method rather than an argument on that one.

        **It returns all the fields, not the one the record spends.**
        ``resolve`` exists to put a single value in a single place, so it
        answers with ``injection.field`` and nothing else. A caller here is
        the code that knows the service: it holds an id beside a secret
        beside a long-lived token, and it decides which of them goes where.

        **It checks no host, because it builds no request.** ``resolve`` can
        promise the address it authenticated is the address that will be
        connected to; nothing above this can promise anything of the sort. The
        caller makes its own calls and can send what it holds anywhere. That
        is the trade ``credentials/reader.py`` states in full, and the reason
        this is reachable only for a record whose injection kind says so.

        Every field must hold something. A partial account is refused rather
        than handed over half-filled, because the code receiving it would ask
        for a name that is not there and fail somewhere further from the
        cause; the refusal names what is missing and where it is typed in.
        """
        entry = self._require(self._load(), credential_id)
        missing = entry.missing_fields()
        if missing:
            raise CredentialNotSet(
                f"{credential_id!r} has nothing stored for "
                f"{', '.join(missing)}. The operator fills those in in "
                f"Settings, Credentials."
            )
        values: dict[str, str] = {}
        for name, field in entry.fields.items():
            assert field.secret is not None
            values[name] = self._open(field.secret, secret_label(credential_id, name))
            if field.seal_stamp is None:
                self._stamp_now(credential_id, name, field.secret)
        return entry.injection, values

    def iter_secrets(self) -> list[tuple[str, str]]:
        """``(label, value)`` for EVERY field holding one. For the redactor only.

        Every field, not the account's primary one. This method is the
        redactor's only source of values, so a version of it that returned one
        string per account would leave every other field unredactable: not
        masked in a tool result, not stripped from the session file, not
        caught in a traceback. Nothing would fail and every sink would open at
        once, which is why the test for this was written before the fields
        were.

        The label is what a person sees in place of the value
        (:func:`secret_label`), so a transcript says which half of a sign-in
        was removed.

        No host check, because there is no host: the caller is not sending
        anything anywhere. It is asking what strings must never be written
        down, which is the opposite question to :meth:`resolve`'s and the
        reason this is a separate method rather than a flag on that one.

        Raises whatever :meth:`_open` raises. A store that cannot be decrypted
        is a store whose values cannot be recognised, and the caller decides
        whether that means failing closed or degrading.
        """
        data = self._load()
        return [
            (
                secret_label(entry.id, name),
                self._open(field.secret, secret_label(entry.id, name)),
            )
            for entry in data.credentials.values()
            for name, field in entry.fields.items()
            if field.secret is not None
        ]

    # ── internals ───────────────────────────────────────────────────────

    @staticmethod
    def _require(data: CredentialFile, credential_id: str) -> Credential:
        entry = data.credentials.get(credential_id)
        if entry is None:
            raise UnknownCredentialError(
                f"no credential registered with id {credential_id!r}"
            )
        return entry

    @staticmethod
    def _seal(value: str) -> str:
        """Encrypt and base64 it, or refuse.

        Both failures are re-raised untouched: DPAPI being absent and DPAPI
        refusing are different problems with different remedies, and the panel
        shows whichever one happened rather than one flattened message.
        """
        if not value.strip():
            raise ValueError("a credential value cannot be blank")
        if len(value) > MAX_VALUE_CHARS:
            # Bounded so that "the output was checked for stored values before
            # it was cut" stays true. Both consumers read a reply or a
            # command's output up to a ceiling and check the whole of what they
            # read; a value LONGER than that ceiling could be cut in half
            # before an exact match could find it, and the leading half would
            # be reported. No real key, token or certificate chain comes near
            # this, and the ceilings are in `runtime.yaml` where they can only
            # go up.
            raise ValueError(
                f"that value is {len(value)} characters, and {MAX_VALUE_CHARS} "
                f"is the most one box holds. Nothing was saved."
            )
        blob = protect(value, _DPAPI_DESCRIPTION)
        return base64.b64encode(blob).decode("ascii")

    @staticmethod
    def _open(sealed: str, label: str) -> str:
        """``label`` is what a person is told about, from :func:`secret_label`."""
        try:
            blob = base64.b64decode(sealed.encode("ascii"), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CredentialStoreError(
                f"{label!r} holds a value that is not a stored blob: {exc}"
            ) from exc
        try:
            return unprotect(blob)
        except (DpapiError, DpapiUnavailable) as exc:
            raise CredentialStoreError(
                f"{label!r} could not be decrypted on this account. A "
                f"credential store is bound to the Windows user that wrote it, "
                f"so a store copied from elsewhere never opens here. Until it "
                f"is cleared, nothing is sent to a model provider, because a "
                f"value that cannot be read is a value that cannot be checked "
                f"for. Clear {label!r} in Settings, Credentials and "
                f"add it again: {exc}"
            ) from exc


__all__ = [
    "CredentialNotSet",
    "CredentialStore",
    "CredentialStoreError",
    "HostNotAllowed",
    "MAX_OPEN_REQUESTS",
    "TooManyFields",
    "TooManyOpenRequests",
    "UnknownCredentialError",
    "UnknownCredentialField",
]
