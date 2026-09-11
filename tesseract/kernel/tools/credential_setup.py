"""credential_setup — say how one of your accounts is spent.

The operator adds an account by typing four things a person knows: what
service it is with, the username, the secret, and what it is for. Everything
else about it is per-service knowledge, and asking a person for it was asking
them to know that one API wants `Authorization: Bearer`, the next wants a
query parameter, and git wants the value inside a web address. That is your
job, not theirs.

This tool writes that half of the record. It never reads a value and there is
nothing here that could return one: the store's decrypting method is not
reachable from a tool at all (GOVERNANCE §1), and this only ever describes
where a value goes.

**Posture `ask`, and the reason is the host list.** The other fields are
mechanics and getting them wrong produces a failed request. The host list is
the one control that stops a value being sent somewhere it was never meant to
go, so a page you read, or an instruction hidden in a document, must not be
able to widen it quietly. The operator sees the hosts you propose and answers
once. That is one yes, not a form.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.credentials.models import PRIMARY_FIELD
from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.kernel.tools.receipt import Receipt


class CredentialSetupInput(BaseModel):
    credential_id: str = Field(
        description=(
            "Which account, from `credential_list`. For example 'cred-github-com'."
        ),
        min_length=1,
        max_length=64,
    )
    allowed_hosts: list[str] = Field(
        description=(
            "The exact hostnames this value may be sent to, for example "
            "['api.github.com']. A subdomain is not covered by its parent, and "
            "anything not listed is refused before a request is built. Name "
            "only what the work needs."
        ),
        min_length=1,
    )
    injection_kind: str = Field(
        default="header",
        description=(
            "Where the value goes when it is spent: 'header' for an API that "
            "wants an Authorization header, 'query' for one that wants it in "
            "the address's query, 'url' for a service authenticated through "
            "the web address itself, which is how git over https works, or "
            "'env' for an account handed whole to code you wrote, which is "
            "what a sign-in wanting several values needs."
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
    injection_field: str = Field(
        default=PRIMARY_FIELD,
        description=(
            "Which of the account's fields is the one that gets sent, from "
            "`credential_list`. Accounts hold one field called 'value' unless "
            "you asked for more, so leave this alone unless you did."
        ),
        min_length=1,
        max_length=40,
    )
    purpose: str = Field(
        default="",
        description=(
            "What you will use it for, if you want to sharpen what the "
            "operator wrote. Leave empty to keep theirs."
        ),
        max_length=500,
    )


class CredentialSetupTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "operator_gate"

    group: ClassVar[str] = "your-own-accounts"
    summary: ClassVar[str] = (
        "Say where one of your accounts' values goes and which hosts it may reach."
    )
    use_when: ClassVar[str] = (
        "Use after the operator adds an account, or when `credential_list` "
        "shows one with no hosts, which cannot be used until this is set. You "
        "know how the service authenticates; they should not have to. Name the "
        "narrowest host list the work needs, because that list is what the "
        "operator is being asked to approve."
    )
    not_when: ClassVar[str] = (
        "asking for an account you do not have, which is `credential_request`, "
        "and reading what you already have, which is `credential_list`. "
        "Neither this nor anything else returns a value."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "record"
    recovery_behaviour: ClassVar[str] = "idempotent"

    @property
    def name(self) -> str:
        return "credential_setup"

    @property
    def input_schema(self) -> type[BaseModel]:
        return CredentialSetupInput

    def is_concurrency_safe(self) -> bool:
        return False

    def is_read_only(self) -> bool:
        return False

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        import asyncio

        from pydantic import ValidationError

        from tesseract.credentials.models import (
            Credential,
            Injection,
            normalise_field_name,
        )
        from tesseract.credentials.store import (
            CredentialStore,
            CredentialStoreError,
            UnknownCredentialError,
            UnknownCredentialField,
        )

        assert isinstance(tool_input, CredentialSetupInput)
        store = CredentialStore()

        try:
            rows = await asyncio.to_thread(store.list_public)
        except CredentialStoreError as exc:
            return ToolResult(output=f"credential_setup: {exc}", is_error=True)

        existing = next(
            (r for r in rows if r["id"] == tool_input.credential_id.strip()), None
        )
        if existing is None:
            known = ", ".join(r["id"] for r in rows) or "none yet"
            return ToolResult(
                output=(
                    f"credential_setup: there is no account called "
                    f"{tool_input.credential_id.strip()!r}. The ones there are: "
                    f"{known}."
                ),
                is_error=True,
            )

        try:
            entry = Credential(
                id=existing["id"],
                service=existing["service"],
                account=existing["account"],
                purpose=tool_input.purpose.strip() or existing["purpose"],
                injection=Injection(
                    kind=tool_input.injection_kind.strip().lower(),  # type: ignore[arg-type]
                    name=tool_input.injection_name.strip(),
                    prefix=tool_input.injection_prefix,
                    field=normalise_field_name(tool_input.injection_field),
                ),
                allowed_hosts=tool_input.allowed_hosts,
                # Untouched by this tool, and carried across so the update
                # cannot silently answer a request the operator has not.
                requested=bool(existing["requested"]),
            )
        except (ValidationError, ValueError) as exc:
            return ToolResult(output=f"credential_setup: {exc}", is_error=True)

        # No field-membership check here any more. The store makes it, once,
        # in `_merge_fields`, which is the funnel every writer goes through:
        # a copy of the rule in this tool left the panel route free to break
        # it, and a record naming a field it does not hold read as ready right
        # up to the spend that failed.

        if not entry.allowed_hosts:
            return ToolResult(
                output=(
                    "credential_setup: name at least one hostname. An account "
                    "with no hosts is refused everywhere rather than allowed "
                    "anywhere."
                ),
                is_error=True,
            )

        try:
            # No values, so whatever is stored stays. This tool has no way to
            # send a value and no way to clear one.
            saved = await asyncio.to_thread(store.put, entry)
        except (UnknownCredentialError, UnknownCredentialField) as exc:
            return ToolResult(output=f"credential_setup: {exc}", is_error=True)
        except (CredentialStoreError, ValueError) as exc:
            return ToolResult(output=f"credential_setup: {exc}", is_error=True)

        from tesseract.orchestrator.panel_refresh import CREDENTIALS_KEY, publish_stale

        publish_stale(CREDENTIALS_KEY)

        where = {
            "header": f"the {saved['injection']['name']} header",
            "query": f"the {saved['injection']['name']} query parameter",
            "env": "your own code, with command_run",
            "url": "the web address itself",
        }[saved["injection"]["kind"]]
        # One sentence per state, because "still waiting for a value" was true
        # of exactly one of them and got said for all of them. A row this call
        # just sealed is ready; the other two are what a store carried in from
        # elsewhere looks like, and each has a different answer.
        from tesseract.credentials.models import SPEND_STATE_WORDS

        ready = SPEND_STATE_WORDS[saved["spend_state"]]
        if saved["spend_state"] == "incomplete":
            still = ", ".join(
                f["label"] for f in saved["fields"] if not f["has_value"]
            )
            ready = f"{ready}: {still}"
        return ToolResult(
            output=(
                f"{saved['id']} ({saved['service']}) is set up: sent as {where} "
                f"to {', '.join(saved['allowed_hosts'])}. It is {ready}."
            ),
            receipt=Receipt(kind="record", id=str(saved["id"])),
            metadata={"credential_id": saved["id"]},
        )


__all__ = ["CredentialSetupInput", "CredentialSetupTool"]
