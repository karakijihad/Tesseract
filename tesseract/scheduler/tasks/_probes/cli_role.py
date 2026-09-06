"""CLI-tier probe — two questions, asked in order, and the record says which.

A `cli`-tier ref is a subscription, and there are two different things that
can be wrong with one.

**Is it signed in?** `providers.yaml` declares an `auth_check` per provider
and `brain/cli_auth.py` runs it. It executes the binary, so it answers about
NOW, and it costs no quota. It is also the only one of the two that can run at
boot.

**Does it still answer?** An auth check cannot see a subscription that is
signed in and out of credit — it passes there, cheerfully, which is how a
nightly report can call a dead account healthy. So when auth says ready, the
provider is asked the cheapest real question there is (`live_check` in
`providers.yaml`) and its refusal becomes the record in its own words.

Once per PROVIDER, not once per ref: two claude refs are one account, and
asking twice spends the thing being measured to learn nothing.

**Every failure says whose it was.** `evidence.origin` is `ours` when the
check never reached the provider (binary missing, spawn refused, timeout) and
`theirs` when the provider answered and refused. One field for both is what
let `binary not found on PATH` be read as "the account is out of usage" for a
whole day, with nothing in the record able to settle it.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Callable, ClassVar

from tesseract.config.loader import CliLiveCheck
from tesseract.kernel.adapters.cli_utils import probe_env, resolve_cli_executable
from tesseract.orchestrator import provider_failure
from tesseract.orchestrator.provider_failure import ProviderFault
from tesseract.scheduler.tasks._probes.base import ProbeResult


class CliProbe:
    """One provider's auth state, then one live call when that state is ready."""

    role_kind: ClassVar[str] = "cli"

    def __init__(
        self,
        *,
        provider: str,
        state_fn: Callable[[str], Any],
        live_check: CliLiveCheck | None,
    ) -> None:
        self._provider = provider
        self._state_fn = state_fn
        # `None` when the caller decided this run must not spend. The probe
        # then reports what the free auth check saw and says the live half was
        # not run, rather than reporting a subscription as verified on a check
        # that cannot see a spent one.
        self._live = live_check
        # One live call per provider per run, even when several refs name it.
        # `provider_probe` builds one instance per provider and then walks
        # REFS, so without this the account is asked the same question once
        # per model entry — spending the quota being measured to learn
        # nothing. `_asked` is the answer, not a flag: a second ref reports
        # the same result rather than a blank one.
        self._asked: tuple[bool, ProviderFault | None] = (False, None)

    async def probe(self, role_name: str, ref: str) -> ProbeResult:
        t0 = time.monotonic()

        state = self._state_fn(self._provider)
        if state is None:
            return self._result(
                role_name, ref, t0, provider_failure.ours("the auth check did not run")
            )
        if getattr(state, "status", "") != "ready":
            # The auth check already attributed this one. Forward its reading
            # rather than composing a second, differently-worded account of
            # the same failure.
            fault = ProviderFault(
                origin=getattr(state, "origin", "") or "theirs",
                kind=getattr(state, "kind", "") or "unknown",
                detail=getattr(state, "reason", None) or "not ready",
            )
            hint = getattr(state, "login_hint", None)
            return self._result(role_name, ref, t0, fault, extra_evidence=
                                {"login_hint": hint} if hint else None)

        if self._live is None:
            return ProbeResult(
                role=role_name, ref=ref, ok=True, drift_kind="none",
                evidence={"auth": "ready", "live": "not run"},
                probed_at=_now(), latency_ms=_ms(t0),
            )

        asked, fault = self._asked
        if not asked:
            fault = await _ask(self._live)
            self._asked = (True, fault)
        if fault is None:
            return ProbeResult(
                role=role_name, ref=ref, ok=True, drift_kind="none",
                evidence={"auth": "ready", "live": "answered"},
                probed_at=_now(), latency_ms=_ms(t0),
            )
        return self._result(role_name, ref, t0, fault)

    def _result(
        self,
        role_name: str,
        ref: str,
        t0: float,
        fault: ProviderFault,
        *,
        extra_evidence: dict[str, Any] | None = None,
    ) -> ProbeResult:
        return ProbeResult(
            role=role_name, ref=ref, ok=False, drift_kind="unavailable",
            evidence=provider_failure.evidence(fault, **(extra_evidence or {})),
            probed_at=_now(), latency_ms=_ms(t0),
        )


async def _ask(live: CliLiveCheck) -> ProviderFault | None:
    """Run the live check. `None` means the provider answered.

    The prompt is the last argument rather than piped: `codex exec` appends
    piped stdin as a `<stdin>` block on top of an argument prompt, and giving
    a subprocess no stdin at all is what stops a CLI that decides to ask
    something from hanging until the timeout.
    """
    argv = (resolve_cli_executable(live.command[0]), *live.command[1:], live.prompt)
    # The probe asks a real CLI a real question, so it is the same shape as a
    # delegate: a process with its own toolbox, running for as long as the
    # check takes, in whatever directory it inherited. With no `cwd` that was
    # the backend's, which nothing sets and which is `app/` on a packaged
    # install. Scheduled, so nobody is watching when it runs.
    from tesseract.orchestrator.seal_guard import safe_cwd

    try:
        # A THREAD, and `subprocess.run` rather than the asyncio spawn, for the
        # reason `brain/cli_auth.py` gives at its own call: on Windows the
        # proactor loop builds its subprocess transport by calling `Popen`
        # inline, so `CreateProcess` runs ON the event loop. Measured on this
        # machine, eight spawns each way: worst single block 47 ms and 60 ms
        # on the loop against 14 ms and 12 ms through a thread. This probe
        # runs a real CLI turn for every role on a schedule, so the blocks
        # serialise, on a backend whose supervisor kills it for not answering
        # a health check.
        completed = await asyncio.to_thread(
            subprocess.run,
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=live.timeout_seconds,
            env=probe_env(live.command[0]),
            cwd=str(safe_cwd(Path.cwd())),
        )
    except FileNotFoundError:
        return provider_failure.ours(f"{live.command[0]} is not on PATH")
    except subprocess.TimeoutExpired:
        # THEIRS. The spawn succeeded, so the provider was reached and said
        # nothing in time — the contract on `ProviderFault` draws the line at
        # dispatch, and `chat_role.py` already draws it there. `run` has
        # already killed the child and waited for it.
        return provider_failure.unanswered(live.timeout_seconds)
    except OSError as exc:
        return provider_failure.ours(f"the call would not start ({type(exc).__name__})")

    stdout = (completed.stdout or b"").decode("utf-8", errors="replace")
    stderr = (completed.stderr or b"").decode("utf-8", errors="replace")

    if completed.returncode == 0:
        # A clean exit is not an answer, and neither is any old text. The whole
        # reason this check exists is that a spent subscription looks healthy;
        # the prompt asks for one specific word so the reply can be CHECKED
        # rather than counted. A refusal is a fluent non-answer, and accepting
        # any non-empty stdout is the same hole the exit code was.
        if live.expect.lower() in stdout.lower():
            return None
        return ProviderFault(
            origin="theirs",
            kind="empty_answer",
            detail=(
                f"exited cleanly without saying {live.expect!r}"
                f"{_reply_tail(stdout) or _stderr_tail(stderr)}"
            ),
        )

    # Both streams. The structured error lands on stdout and the plain text on
    # stderr, and which one carries the cause depends on how far the CLI got.
    fault = provider_failure.theirs(stdout + "\n" + stderr)
    if fault.kind == "unknown" and not stdout.strip():
        # OURS. A non-zero exit with nothing on stdout means the CLI never got
        # as far as a turn — a rejected flag, a broken install, a sandbox it
        # would not start in. Calling that the provider's refusal invents a
        # provider response that was never received, which is the whole defect
        # this module exists to stop.
        return provider_failure.ours(
            f"{live.command[0]} exited {completed.returncode} before reaching the "
            f"provider{_stderr_tail(stderr)}"
        )
    return fault


def _reply_tail(stdout: str) -> str:
    """What it said instead of the word it was asked for."""
    flat = " ".join(stdout.split())
    return f" (said: {flat[-120:]})" if flat else ""


def _stderr_tail(stderr: str) -> str:
    """What the CLI muttered while saying nothing, if anything.

    A silent success is the hardest failure to act on, so the little that was
    on the other stream is worth carrying even though it is not an answer.
    """
    flat = " ".join(stderr.split())
    return f" (stderr: {flat[-120:]})" if flat else ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ms(t0: float) -> float:
    return (time.monotonic() - t0) * 1000.0


__all__ = ["CliProbe"]
