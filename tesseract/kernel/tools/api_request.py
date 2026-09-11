"""api_request — call a web service with one of your own accounts.

This is the general one. It knows no service, has no list of the APIs it
supports, and nothing about it needs changing when you want to use a new one:
you name the account, you write the request that service's documentation
describes, and the runtime puts the value where the account's record says it
goes. That is what makes "here is an account, go build what you need" true.

You never see the value, before or after. `credentials/spend.py` builds the
request, this sends it, and the reply is checked for stored values on its way
back into the conversation, because a service that echoes a token into an
error body would otherwise put it in the transcript without anyone choosing to.

**Redirects are not followed.** The account's allowlist was checked against
the address you gave; a redirect names a different one after that check has
passed. You are told where it points and you can ask again.

**The reply is checked before it is cut, and cut before it is reported.** That
order is the whole of it. The check is an exact match on the stored value, so
a service that padded its answer until the value straddled the cut would leave
a fragment nothing could find afterwards, and the wall further up would see
only what was left. So the body is read to a ceiling, checked whole, and
shortened last.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult

_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def _limits() -> tuple[float, int, int]:
    """How long to wait, how much to read, how much to report.

    All three live in `runtime.yaml` and are read at call time, which is the
    project rule and is also what lets an operator change them without a
    restart. The reader refuses a report ceiling that is not below the read
    ceiling: the read ceiling is itself a cut, and a value straddling it can
    only stay out of the report while the report is the shorter of the two.
    """
    from tesseract.config.runtime_limits import (
        default_runtime_config_path,
        load_api_request_max_body_bytes,
        load_api_request_report_chars,
        load_api_request_timeout_s,
    )

    path = default_runtime_config_path()
    return (
        load_api_request_timeout_s(path),
        load_api_request_max_body_bytes(path),
        load_api_request_report_chars(path),
    )


class ApiRequestInput(BaseModel):
    credential_id: str = Field(
        description=(
            "Which of your accounts pays for this call, from `credential_list`."
        ),
        min_length=1,
        max_length=64,
    )
    url: str = Field(
        description=(
            "The full https address, as the service's documentation writes it. "
            "Its host must be one the account names, or nothing is sent."
        ),
        min_length=1,
        max_length=2000,
    )
    method: str = Field(
        default="GET",
        description="GET, POST, PUT, PATCH or DELETE.",
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Any headers the service wants. Leave out the one the account is "
            "sent in; setting it yourself is refused rather than overwritten."
        ),
    )
    body: str = Field(
        default="",
        description=(
            "The request body, written out. JSON is assumed when you send one "
            "and name no content type."
        ),
        max_length=500_000,
    )


def _without_the_value(exc: Exception) -> str:
    """What a transport failure says, with no stored value left in it.

    A query-injection account puts its value in the address, and several httpx
    errors quote the address they failed on. `execute_tool` redacts every tool
    result on its way into the conversation and would catch it, but this tool
    is the one that knowingly holds a value, so it does not export the problem
    to a wall two layers up. If the store cannot be read the value cannot be
    looked for, and then the class of failure is all this may say.
    """
    from tesseract.credentials.redaction import RedactionUnavailable, redact

    try:
        return redact(str(exc))
    except RedactionUnavailable:
        return type(exc).__name__


def _reportable(text: str, ceiling: int) -> str:
    """The reply, checked for stored values, then cut to what is reported.

    The ORDER is the point, and it is not a preference. `redact` matches a
    stored value exactly, so a service that pads its answer until the value
    straddles the cut leaves two fragments and an exact match finds neither.
    Cutting first would hand the wall at `execute_tool` a string the value had
    already been destroyed in, and the leak would be invisible everywhere
    downstream.

    A store that cannot be read means the reply cannot be checked, and an
    unchecked reply from a service holding one of these accounts is not shown
    at all. That is the same fail-closed choice the provider boundary makes,
    for the same reason: this is the one place the value could be coming back.
    """
    from tesseract.credentials.redaction import RedactionUnavailable, redact

    try:
        checked = redact(text)
    except RedactionUnavailable as exc:
        return (
            f"[the reply is not shown: the credential store could not be read, "
            f"so it could not be checked for stored values. {exc}]"
        )
    if len(checked) <= ceiling:
        return checked
    return checked[:ceiling] + f"\n[... {len(checked) - ceiling} more characters]"


class ApiRequestTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "operator_gate"

    group: ClassVar[str] = "your-own-accounts"
    summary: ClassVar[str] = (
        "Call a web service with one of your own accounts, without seeing the value."
    )
    use_when: ClassVar[str] = (
        "Use for any service you have an account for: creating a repository, "
        "sending a message, reading something back. Check `credential_list` "
        "for the account and what it may be used for, write the request the "
        "way that service documents it, and leave the account's value out. If "
        "the account has no hosts yet, `credential_setup` is what fixes that."
    )
    not_when: ClassVar[str] = (
        "reading an ordinary web page, which is `web_search` or a fetch and "
        "needs no account. Pushing and pulling, which git does with its own "
        "account. And anything you would have to put a value into by hand, "
        "which cannot be done here and is the point."
    )
    depends_on: ClassVar[str] = ""
    # The far side may mint an id and may not, and the shape differs per
    # service, so nothing here can name one honestly. The call and its
    # response are what the turn record keeps.
    receipt_kind: ClassVar[str] = "none"
    recovery_behaviour: ClassVar[str] = "unsafe"

    @property
    def name(self) -> str:
        return "api_request"

    @property
    def input_schema(self) -> type[BaseModel]:
        return ApiRequestInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return False

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        import httpx

        from tesseract import http_client
        from tesseract.credentials.spend import (
            DestinationAlreadyAuthenticated,
            InjectionPointNotAllowed,
            InsecureDestination,
            NotSpendableOverHttp,
            authenticate,
        )
        from tesseract.credentials.store import (
            CredentialNotSet,
            CredentialStoreError,
            HostNotAllowed,
            UnknownCredentialError,
        )

        assert isinstance(tool_input, ApiRequestInput)
        timeout_s, max_body_bytes, report_chars = _limits()
        method = tool_input.method.strip().upper()
        if method not in _METHODS:
            return ToolResult(
                output=(
                    f"api_request: {tool_input.method!r} is not a method this "
                    f"sends. It does {', '.join(_METHODS)}."
                ),
                is_error=True,
            )

        headers = dict(tool_input.headers)
        content = tool_input.body.encode("utf-8") if tool_input.body else None
        if content is not None and not any(
            key.strip().lower() == "content-type" for key in headers
        ):
            headers["Content-Type"] = "application/json"

        try:
            prepared = authenticate(
                tool_input.url.strip(),
                headers,
                credential_id=tool_input.credential_id.strip(),
            )
        except (
            CredentialNotSet,
            CredentialStoreError,
            DestinationAlreadyAuthenticated,
            HostNotAllowed,
            InjectionPointNotAllowed,
            InsecureDestination,
            NotSpendableOverHttp,
            UnknownCredentialError,
        ) as exc:
            return ToolResult(output=f"api_request: {exc}", is_error=True)

        try:
            async with http_client.async_client(
                timeout=timeout_s, follow_redirects=False
            ) as client:
                async with client.stream(
                    method,
                    prepared.url,
                    headers=prepared.headers,
                    content=content,
                ) as response:
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) >= max_body_bytes:
                            # Cut what was KEPT, not only the loop. A chunk is
                            # whatever size it arrives in, so breaking without
                            # this holds a reply of any size the moment it
                            # comes in one piece.
                            del body[max_body_bytes:]
                            break
                    status = response.status_code
                    is_redirect = response.is_redirect
                    is_error = response.is_error
                    location = response.headers.get("location", "")
                    charset = response.charset_encoding or "utf-8"
        except httpx.InvalidURL as exc:
            # Named on its own because it inherits from `Exception` rather
            # than from `httpx.HTTPError`, so the clause below does not cover
            # it and it would leave this tool by a route that says less.
            return ToolResult(
                output=(
                    f"api_request: {prepared.safe_url} is not an address that "
                    f"can be sent: {_without_the_value(exc)}"
                ),
                is_error=True,
            )
        except httpx.TimeoutException:
            return ToolResult(
                output=(
                    f"api_request: {prepared.safe_url} did not answer within "
                    f"{int(timeout_s)} seconds. Whether it acted on the "
                    f"request is not known from here, so check before sending "
                    f"it again."
                ),
                is_error=True,
            )
        except httpx.HTTPError as exc:
            return ToolResult(
                output=(
                    f"api_request: {prepared.safe_url} could not be reached: "
                    f"{_without_the_value(exc)}"
                ),
                is_error=True,
            )

        # `prepared` holds the value in a header or in its url. Nothing below
        # reads either: what is reported is the address as it was given.
        text = _reportable(bytes(body).decode(charset, errors="replace"), report_chars)
        metadata: dict[str, Any] = {
            "status": status,
            "url": prepared.safe_url,
            "credential_id": tool_input.credential_id.strip(),
        }

        if is_redirect and location:
            return ToolResult(
                output=(
                    f"{status} from {prepared.safe_url}, redirected to "
                    f"{location}. It was not followed, because the account was "
                    f"cleared for the address you gave and that one names "
                    f"another. Ask again for the new address if the account "
                    f"covers its host.\n\n{text}"
                ),
                metadata={**metadata, "redirect_to": location},
            )

        return ToolResult(
            output=f"{status} from {prepared.safe_url}\n\n{text}",
            is_error=is_error,
            metadata=metadata,
        )


__all__ = ["ApiRequestInput", "ApiRequestTool"]
