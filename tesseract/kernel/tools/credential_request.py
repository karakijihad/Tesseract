"""credential_request — ask the operator for what an account needs.

Two asks, one tool, and which one you get depends on whether you name an
account you already have:

* **no account named** — a whole new account. A row goes into the credential
  store with nothing in it, the operator sees it in Settings, Credentials as
  a request waiting on them, fills it in, and the row becomes usable.
* **an account named** — more FIELDS on that account. A sign-in is often a
  set rather than a single token, and an account that turns out to want an id
  beside its secret used to leave a run with nowhere to go but a report. Now
  it asks, the panel grows a box per field, and the operator types them in.

Nothing is sent anywhere and nothing runs, either way.

Posture `auto`, on the same reasoning `propose_change` carries: the row IS the
operator gate. A chat prompt in front of it would be a second approval for the
same decision, and the request is worth nothing until a person types a value
into it anyway.

**A field ask cannot carry a destination**, and it cannot quietly carry
anything else either. Naming an account refuses EVERY input that belongs to the
other branch, not only the two that touch a destination: refusing two and
silently dropping five made the separation structural for a third of the schema
and invisible for the rest, and a caller correcting an account name got no sign
that nothing had happened. Widening is `credential_setup`, which is `ask`. A
field name is a description of what a service wants; a host is a place a value
goes.

There is deliberately no `credential_use` beside this. A value is resolved by
the runtime at the request site, never by a tool, so there is nothing for the
model to call and nothing for a tool result to carry (GOVERNANCE §1).
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.kernel.tools.receipt import Receipt


class CredentialRequestInput(BaseModel):
    credential_id: str = Field(
        default="",
        description=(
            "An account you ALREADY have, from `credential_list`, when what "
            "you need is more values on it rather than a new account. Give "
            "this with `fields` and leave everything else out. Empty asks for "
            "a new account instead."
        ),
        max_length=64,
    )
    fields: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "The values the service asks you for, as a short name mapped to "
            "the wording the operator will read above the box, for example "
            "{'client_secret': 'Client secret'}. Names are lowercase words "
            "joined by underscores. Leave empty for an account that needs "
            "only one value."
        ),
    )
    service: str = Field(
        default="",
        description=(
            "The service the account is with, e.g. 'Cloudflare'. Required "
            "unless you named an account."
        ),
        max_length=120,
    )
    purpose: str = Field(
        default="",
        description=(
            "What you need it for, in one sentence the operator can decide "
            "on. Required unless you named an account."
        ),
        max_length=500,
    )
    allowed_hosts: list[str] = Field(
        default_factory=list,
        description=(
            "The hostnames the value may be sent to, e.g. "
            "['api.cloudflare.com']. Exact hostnames: a subdomain is not "
            "covered by its parent. Required unless you named an account, and "
            "refused if you did."
        ),
    )
    injection_kind: str = Field(
        default="header",
        description=(
            "Where the value goes: 'header' for a service wanting an "
            "Authorization header, 'query' for one wanting it in the "
            "address's query, 'url' for a service authenticated through the "
            "web address itself, which is how git over https works, or 'env' "
            "for an account handed whole to code you wrote, which is what a "
            "sign-in wanting several values needs."
        ),
    )
    injection_name: str = Field(
        default="Authorization",
        description=(
            "What that place is called: the header name, the query parameter "
            "name, or, for 'url', the username that goes before the value in "
            "the address. Leave it empty for 'env', which hands over every "
            "field under its own name and so has no single place to point at."
        ),
        max_length=128,
    )
    injection_prefix: str = Field(
        default="Bearer ",
        description=(
            "What goes in front of the value, if the service wants something. "
            "'Bearer ' and 'token ' are the common ones. Empty for most, and "
            "always empty for 'url', which has nowhere to put one."
        ),
        max_length=32,
    )
    account: str = Field(
        default="",
        description="The username or account name, if you know it.",
        max_length=200,
    )
    injection_field: str = Field(
        default="",
        description=(
            "For a NEW account asking for several values: which of them is "
            "the one actually sent. Leave empty unless `fields` names more "
            "than one, in which case name one of them."
        ),
        max_length=40,
    )


def _tell_the_panel() -> None:
    """Say the credential listing is out of date, to any open panel.

    A live run added three fields and the operator saw the row exactly as it
    had been until they reloaded the window by hand. The row IS the gate this
    tool's `auto` posture rests on, so a gate the operator cannot see appear
    is the whole mechanism failing quietly.
    """
    from tesseract.orchestrator.panel_refresh import CREDENTIALS_KEY, publish_stale

    publish_stale(CREDENTIALS_KEY)


def _spent_field(tool_input: "CredentialRequestInput") -> dict[str, str]:
    """``{"field": name}`` for the value this new account actually sends.

    Empty when there is nothing to say, so `Injection` keeps its own default.
    A single named field is unambiguous and is taken without being asked for,
    because making the model repeat itself is how a call fails on a formality.
    """
    named = tool_input.injection_field.strip()
    if named:
        return {"field": named}
    if len(tool_input.fields) == 1:
        return {"field": next(iter(tool_input.fields))}
    return {}


class CredentialRequestTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "your-own-accounts"
    summary: ClassVar[str] = (
        "Ask the operator for an account you do not have, or for more of what "
        "one needs."
    )
    use_when: ClassVar[str] = (
        "Use when a task needs an account of your own and `credential_list` "
        "shows none for that service, or when one you have lacks a field it "
        "asks for. That looks like an account reading `ready` and being "
        "refused anyway: ready means a value is stored, never that the "
        "service accepts it. Ask for the missing pieces by name, and say so "
        "in the same reply."
    )
    not_when: ClassVar[str] = (
        "the operator's own accounts, which are theirs to hold. Widening "
        "where an account may be sent, which is `credential_setup`. Asking "
        "twice for one already waiting on them changes nothing: "
        "`credential_list` shows you what is outstanding."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "record"
    recovery_behaviour: ClassVar[str] = "idempotent"

    @property
    def name(self) -> str:
        return "credential_request"

    @property
    def input_schema(self) -> type[BaseModel]:
        return CredentialRequestInput

    def is_concurrency_safe(self) -> bool:
        return False

    def is_read_only(self) -> bool:
        return False

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        assert isinstance(tool_input, CredentialRequestInput)
        if tool_input.credential_id.strip():
            return await self._ask_for_fields(tool_input)
        return await self._ask_for_an_account(tool_input)

    @staticmethod
    async def _ask_for_fields(tool_input: CredentialRequestInput) -> ToolResult:
        """More boxes on an account that already exists."""
        import asyncio

        from tesseract.credentials.store import (
            CredentialStore,
            CredentialStoreError,
            TooManyFields,
            UnknownCredentialError,
            UnknownCredentialField,
        )

        credential_id = tool_input.credential_id.strip()
        # Every input that means something only to the other branch, not just
        # the two that touch a destination. Refusing two and silently dropping
        # five made the separation structural for a third of the schema and
        # invisible for the rest, and a caller correcting an account name here
        # got no sign that nothing had happened.
        blank = CredentialRequestInput()
        ignored = [
            name
            for name in (
                "service",
                "purpose",
                "allowed_hosts",
                "account",
                "injection_kind",
                "injection_name",
                "injection_prefix",
            )
            if getattr(tool_input, name) != getattr(blank, name)
        ]
        if ignored:
            return ToolResult(
                output=(
                    f"credential_request: naming an account asks for more "
                    f"values on it and nothing else, so {', '.join(ignored)} "
                    f"would have been ignored. Where an account may be sent "
                    f"and how it is spent are credential_setup, which needs "
                    f"the operator's answer. Nothing was recorded."
                ),
                is_error=True,
            )
        if not tool_input.fields:
            return ToolResult(
                output=(
                    "credential_request: name the fields you need on "
                    f"{credential_id}, as a short name mapped to the wording "
                    "the operator reads, for example "
                    "{'client_secret': 'Client secret'}."
                ),
                is_error=True,
            )

        try:
            entry = await asyncio.to_thread(
                CredentialStore().request_fields, credential_id, tool_input.fields
            )
        except UnknownCredentialError as exc:
            return ToolResult(output=f"credential_request: {exc}", is_error=True)
        except (
            TooManyFields,
            CredentialStoreError,
            UnknownCredentialField,
            ValueError,
        ) as exc:
            return ToolResult(output=f"credential_request: {exc}", is_error=True)

        _tell_the_panel()
        missing = [f["label"] for f in entry["fields"] if not f["has_value"]]
        if not missing:
            return ToolResult(
                output=(
                    f"{credential_id} already holds every field you asked "
                    f"for, so there is nothing waiting on the operator."
                ),
                receipt=Receipt.nothing(),
                metadata={"credential": entry, "asked": False},
            )
        return ToolResult(
            output=(
                f"Asked for {len(missing)} more on {credential_id}: "
                f"{', '.join(missing)}. They are waiting for the operator in "
                f"Settings, Credentials. Tell them what each one is and where "
                f"to get it, because the box only carries its name."
            ),
            # The credential's own id, which `credential_list` resolves. The
            # values live in the vault and a receipt never points at those.
            receipt=Receipt(kind="record", id=str(entry["id"])),
            metadata={"credential": entry, "asked": True},
        )

    @staticmethod
    async def _ask_for_an_account(tool_input: CredentialRequestInput) -> ToolResult:
        """A whole account nobody has yet."""
        import asyncio

        from pydantic import ValidationError

        from tesseract.credentials.models import CredentialDraft, Injection
        from tesseract.credentials.store import (
            CredentialStore,
            CredentialStoreError,
            TooManyOpenRequests,
            UnknownCredentialField,
        )

        service = tool_input.service.strip()
        if not service or not tool_input.purpose.strip():
            return ToolResult(
                output=(
                    "credential_request: say which service the account is "
                    "with and what you need it for. To ask for more values on "
                    "an account you already have, name it in credential_id "
                    "instead."
                ),
                is_error=True,
            )

        try:
            draft = CredentialDraft(
                service=service,
                account=tool_input.account.strip(),
                purpose=tool_input.purpose.strip(),
                injection=Injection(
                    kind=tool_input.injection_kind.strip().lower(),  # type: ignore[arg-type]
                    name=tool_input.injection_name.strip(),
                    prefix=tool_input.injection_prefix,
                    # Named, or the only field asked for, or the one every
                    # account has. Without this an account asking for two
                    # values could never be created at all: the default field
                    # was one the caller had not asked for, and the store
                    # refuses that record for the reason it should.
                    **_spent_field(tool_input),
                ),
                allowed_hosts=tool_input.allowed_hosts,
                fields=tool_input.fields,
            )
        except (ValidationError, ValueError) as exc:
            return ToolResult(output=f"credential_request: {exc}", is_error=True)

        if not draft.allowed_hosts:
            return ToolResult(
                output=(
                    "credential_request: name at least one hostname the value "
                    "may be sent to. A credential with no hosts cannot be used."
                ),
                is_error=True,
            )

        try:
            entry, created = await asyncio.to_thread(CredentialStore().request, draft)
        except (
            TooManyOpenRequests,
            CredentialStoreError,
            UnknownCredentialField,
        ) as exc:
            # `UnknownCredentialField` is a KeyError, not a CredentialStoreError,
            # so it used to leave this tool as a traceback while every other
            # store refusal arrived as a sentence written for the operator.
            return ToolResult(output=f"credential_request: {exc}", is_error=True)

        _tell_the_panel()

        # Keyed on `spend_state`, not on `has_value` and not on `requested`. A
        # row the operator cleared has neither flag set, and a row holding two
        # of the four values it needs has `has_value` set while being unusable;
        # reading either one alone once told the assistant an account was ready.
        if not created:
            from tesseract.credentials.models import SPEND_STATE_WORDS

            state = SPEND_STATE_WORDS[entry["spend_state"]]
            return ToolResult(
                output=(
                    f"You already have a {service} account ({entry['id']}): "
                    f"{state}."
                ),
                receipt=Receipt.nothing(),
                metadata={"credential": entry, "created": False},
            )

        return ToolResult(
            output=(
                f"Asked for a {service} account ({entry['id']}). It is waiting "
                f"for the operator in Settings, Credentials. Tell them what you "
                f"need it for so they know to go and add it."
            ),
            receipt=Receipt(kind="record", id=str(entry["id"])),
            metadata={"credential": entry, "created": True},
        )


__all__ = ["CredentialRequestInput", "CredentialRequestTool"]
