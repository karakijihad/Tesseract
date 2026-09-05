"""Process-wide cache of CLI subscription auth state.

`cli`-tier providers (claude, codex) authenticate via a subscription login,
not an API key — `providers.yaml`'s `auth_check` block (see
`tesseract/config/loader.py::CliAuthCheck`) declares how to probe each one.
This module owns the probe and the cache that `capabilities.py` (report) and
the delegate call site (use-time invalidation) both read.

Key rules:

- **PII (§6, non-negotiable).** `claude auth status` returns the operator's
  account email, org id, and org name in stdout. `_probe_one` evaluates
  `success_pattern` against both streams and discards them immediately —
  only a status string, a short authored `reason`, a fault kind, and the
  configured `login_hint` survive into the cache, any API response, or any
  log line. Never at debug level. This is the one probe whose reason is
  authored rather than quoted, because its output is an identity; the live
  check quotes the provider instead.
- **Never raises (§7).** Timeout, non-zero exit, missing binary, and any
  other subprocess failure all collapse to an `unavailable` `CliAuthState`.
  A probe failure must never break boot or a settings-read route.
- **Cache is process-wide, not per-request.** `refresh()` re-probes every
  enabled `cli` provider concurrently (`asyncio.gather(...,
  return_exceptions=True)`) so one hung/broken CLI never blocks the others.
  `invalidate()` drops one provider (use-time failure) or the whole cache
  (explicit Verify).
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time
from dataclasses import dataclass

from tesseract.config.loader import CliAuthCheck, ConfigBundle
from tesseract.kernel.adapters.cli_utils import probe_env, resolve_cli_executable
from tesseract.orchestrator import provider_failure
from tesseract.orchestrator.provider_failure import ProviderFault

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CliAuthState:
    """One provider's cached auth-probe result. No raw probe output ever
    lands here — see the module docstring's PII rule."""

    status: str  # "ready" | "unavailable"
    reason: str | None
    login_hint: str | None
    checked_at: float
    # Whose fault the failure was, and what shape it had. `ours` means the
    # check never reached the provider, so nothing about the subscription is
    # known; `theirs` means the provider answered and refused. Empty on a
    # `ready` state.
    #
    # These are separate from `reason` because one field for both is what made
    # `binary not found on PATH` readable as "the account is out of usage" for
    # a whole day — see `orchestrator/provider_failure.py`.
    origin: str = ""
    kind: str = ""


# What a status command's failure means, in words a person can act on. The
# kind comes from the classifier; only the phrasing is ours, and only because
# the text it was classified from cannot be stored.
# Total over `provider_failure.CLASSIFIED_KINDS`, and a test holds it that
# way: this is the one path that cannot quote the provider, so a kind with no
# reading authored for it is a `KeyError` where the operator would otherwise
# have been told something.
_READING = {
    "usage": "signed in, but the subscription has no usage left",
    "not_found": "signed in, but the provider has not got what was asked for",
    "rate_limit": "signed in, but the provider is turning calls away for now",
    "schema": "signed in, but the check itself was rejected as malformed",
    "server": "signed in, but the provider could not be reached",
    "auth": "installed, not signed in",
    "unknown": "installed, not signed in",
}

_cache: dict[str, CliAuthState] = {}


def _cli_connections(bundle: ConfigBundle) -> dict[str, CliAuthCheck]:
    """One `auth_check` per enabled `cli`-tier provider name.

    Reuses `ConfigBundle.all_models()` (already resolves every connection
    via `tesseract/config/loader.py::_build_connection`) instead of
    re-parsing `providers_raw` here — single source of parsing truth.
    """
    out: dict[str, CliAuthCheck] = {}
    for _ref, conn, _model in bundle.all_models():
        if conn.tier != "cli" or conn.name in out:
            continue
        if not conn.tier_enabled or not conn.enabled:
            continue
        if conn.auth_check is not None:
            out[conn.name] = conn.auth_check
    return out


def _failed(check: CliAuthCheck, fault: ProviderFault) -> CliAuthState:
    return CliAuthState(
        status="unavailable",
        reason=fault.line,
        login_hint=check.login_hint,
        checked_at=time.time(),
        origin=fault.origin,
        kind=fault.kind,
    )


async def _probe_one(check: CliAuthCheck) -> CliAuthState:
    """Run `check.command`, match `success_pattern`, discard the output.

    Never raises — every failure mode (missing binary, timeout, OS error)
    becomes an `unavailable` state with a short, PII-free reason.

    **This check's reason is authored, and that is deliberate.** Elsewhere the
    rule is that the provider's own words are the record; here the output is
    an account dump (§6) that cannot be stored, so both streams are read, the
    kind is classified from them, and the text is dropped. The provider's
    verbatim sentence comes from the live check instead
    (`scheduler/tasks/_probes/cli_role.py`), where the output is an error
    stream rather than an identity.
    """
    argv = (resolve_cli_executable(check.command[0]), *check.command[1:])
    try:
        # A THREAD, and `subprocess.run` rather than the asyncio spawn this
        # used. `asyncio.create_subprocess_exec` is the right call on POSIX and
        # a trap on Windows: the proactor loop builds the transport by calling
        # `Popen` inline, so `CreateProcess` runs ON the event loop. The
        # `gather` below cannot help with that — the spawns serialise on the
        # loop and each one blocks it, which is why probing several CLIs at
        # boot showed up as 23 samples inside `_execute_child` and 7.7 seconds
        # of lag, on a machine whose supervisor kills the backend for not
        # answering a health check.
        #
        # `run` puts the spawn, the wait and both reads on the worker thread.
        # It kills the child on timeout the same way this did by hand.
        completed = await asyncio.to_thread(
            subprocess.run,
            argv,
            capture_output=True,
            timeout=check.timeout_seconds,
            # A scrubbed environment, not an inherited one. This spawned the
            # configured binary holding every API key and every MCP bearer
            # token on the machine, on a schedule, with nothing in the way.
            env=probe_env(check.command[0]),
        )
    except FileNotFoundError:
        return _failed(check, provider_failure.ours(f"{check.command[0]} is not on PATH"))
    except subprocess.TimeoutExpired:
        return _failed(
            check, provider_failure.ours(f"the probe timed out after {check.timeout_seconds:g}s")
        )
    except OSError as exc:
        return _failed(
            check, provider_failure.ours(f"the probe would not start ({type(exc).__name__})")
        )

    stdout_bytes = completed.stdout or b""
    stderr_bytes = completed.stderr or b""

    # BOTH streams, and this is measured, not defensive. `claude auth status`
    # prints its JSON to stdout; `codex login status` prints `Logged in using
    # ChatGPT` to STDERR and leaves stdout empty. Matching stdout alone read a
    # signed-in subscription as `installed, not signed in` — the same wrong
    # answer the unresolvable binary gave, arriving by a second route.
    #
    # PII (§6): evaluate the pattern and classify, then let both streams fall
    # out of scope — never stored, returned, or logged, including at debug
    # level. `classify` returns one word from a closed list; nothing it was
    # given survives the call.
    output = (
        stdout_bytes.decode("utf-8", errors="replace")
        + "\n"
        + stderr_bytes.decode("utf-8", errors="replace")
    )
    if re.search(check.success_pattern, output) is not None:
        return CliAuthState(status="ready", reason=None, login_hint=None, checked_at=time.time())

    kind = provider_failure.classify(output)
    if kind == "unknown" and not output.strip():
        # A process that printed nothing answered nothing, whatever it exited
        # with. Deciding this on the exit code instead put `1` — the most
        # common failure code there is — back on the wrong side of the line,
        # and `installed, not signed in` is a claim about the account that a
        # silent process does not support.
        return _failed(
            check,
            provider_failure.ours(
                f"{check.command[0]} exited {completed.returncode} without answering"
            ),
        )
    return _failed(check, ProviderFault(origin="theirs", kind=kind, detail=_READING[kind]))


async def refresh(bundle: ConfigBundle | None = None) -> dict[str, CliAuthState]:
    """Re-probe every enabled `cli` provider and replace the cache wholesale.

    One provider's probe raising (should not happen — `_probe_one` never
    raises, but `asyncio.gather(return_exceptions=True)` is the belt-and-
    suspenders backstop that keeps one failure isolated) never
    blocks the others.
    """
    if bundle is None:
        from tesseract.brain.boot import load_bundle

        bundle = load_bundle()

    connections = _cli_connections(bundle)
    if not connections:
        _cache.clear()
        return {}

    names = list(connections)
    results = await asyncio.gather(
        *(_probe_one(connections[name]) for name in names),
        return_exceptions=True,
    )

    fresh: dict[str, CliAuthState] = {}
    for name, result in zip(names, results):
        if isinstance(result, BaseException):
            log.warning("cli_auth: probe for %r raised — treating as unavailable", name, exc_info=result)
            fresh[name] = _failed(
                connections[name],
                provider_failure.ours(f"the probe raised {type(result).__name__}"),
            )
        else:
            fresh[name] = result

    _cache.clear()
    _cache.update(fresh)
    return dict(fresh)


def auth_checked_providers(bundle: ConfigBundle) -> set[str]:
    """Every enabled `cli` provider that declares an `auth_check`.

    The provider probe asks this before deciding a ref can be checked without
    a model call; reading it here keeps one implementation of "which providers
    authenticate by subscription" rather than a second walk over the catalog.
    """
    return set(_cli_connections(bundle))


def get(provider: str) -> CliAuthState | None:
    """Cached state for one `cli` provider name, or `None` if never probed."""
    return _cache.get(provider)


def snapshot() -> dict[str, CliAuthState]:
    """Copy of the full cache — one entry per provider last probed."""
    return dict(_cache)


def invalidate(provider: str | None = None) -> None:
    """Drop one provider's cached state, or the whole cache when `provider`
    is `None`. Does not re-probe — callers that need a fresh read call
    `refresh()` afterward (the reverify route does both)."""
    if provider is None:
        _cache.clear()
    else:
        _cache.pop(provider, None)


__all__ = [
    "CliAuthState",
    "auth_checked_providers",
    "refresh",
    "get",
    "snapshot",
    "invalidate",
]
