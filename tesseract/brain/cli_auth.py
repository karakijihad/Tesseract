"""Process-wide cache of CLI subscription auth state.

`cli`-tier providers (claude, codex) authenticate via a subscription login,
not an API key — `providers.yaml`'s `auth_check` block (see
`tesseract/config/loader.py::CliAuthCheck`) declares how to probe each one.
This module owns the probe and the cache that `capabilities.py` (report) and
the delegate call site (use-time invalidation) both read.

See `Docs/Plan/cli-auth/DESIGN.md` for the full contract. Key rules:

- **PII (§6, non-negotiable).** `claude auth status` returns the operator's
  account email, org id, and org name in stdout. `_probe_one` evaluates
  `success_pattern` against stdout and discards it immediately — only a
  status string, a short `reason`, and the configured `login_hint` survive
  into the cache, any API response, or any log line. Never at debug level.
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
import time
from dataclasses import dataclass

from tesseract.config.loader import CliAuthCheck, ConfigBundle

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CliAuthState:
    """One provider's cached auth-probe result. No raw probe output ever
    lands here — see the module docstring's PII rule."""

    status: str  # "ready" | "unavailable"
    reason: str | None
    login_hint: str | None
    checked_at: float


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


async def _probe_one(check: CliAuthCheck) -> CliAuthState:
    """Run `check.command`, match `success_pattern`, discard stdout.

    Never raises — every failure mode (missing binary, timeout, OS error)
    becomes an `unavailable` state with a short, PII-free reason.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *check.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return CliAuthState(
            status="unavailable",
            reason="binary not found on PATH",
            login_hint=check.login_hint,
            checked_at=time.time(),
        )
    except OSError as exc:
        return CliAuthState(
            status="unavailable",
            reason=f"auth probe failed to start: {type(exc).__name__}",
            login_hint=check.login_hint,
            checked_at=time.time(),
        )

    try:
        stdout_bytes, _stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=check.timeout_seconds
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return CliAuthState(
            status="unavailable",
            reason="auth probe timed out",
            login_hint=check.login_hint,
            checked_at=time.time(),
        )

    # PII (§6): evaluate the pattern, then let stdout fall out of scope —
    # never stored, returned, or logged, including at debug level.
    matched = re.search(check.success_pattern, stdout_bytes.decode("utf-8", errors="replace")) is not None

    if matched:
        return CliAuthState(status="ready", reason=None, login_hint=None, checked_at=time.time())
    return CliAuthState(
        status="unavailable",
        reason="installed, not signed in",
        login_hint=check.login_hint,
        checked_at=time.time(),
    )


async def refresh(bundle: ConfigBundle | None = None) -> dict[str, CliAuthState]:
    """Re-probe every enabled `cli` provider and replace the cache wholesale.

    One provider's probe raising (should not happen — `_probe_one` never
    raises, but `asyncio.gather(return_exceptions=True)` is the belt-and-
    suspenders backstop per CLAUDE.md's failure-isolation rule) never
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
            fresh[name] = CliAuthState(
                status="unavailable",
                reason=f"probe error: {type(result).__name__}",
                login_hint=connections[name].login_hint,
                checked_at=time.time(),
            )
        else:
            fresh[name] = result

    _cache.clear()
    _cache.update(fresh)
    return dict(fresh)


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


__all__ = ["CliAuthState", "refresh", "get", "snapshot", "invalidate"]
