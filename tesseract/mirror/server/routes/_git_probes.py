"""What this machine can answer about git, and how long it is given to answer.

The primitives the Git panel's three modules share: one read-only subprocess
runner, the cache in front of the two probes that describe the MACHINE, and
the deadline every filesystem question about a project root runs under.

Split out of ``settings_git.py`` because these are the only things in that
panel with no opinion about identity or the registry. `settings_git` asks them
what the machine has, `_git_projects` asks them whether a root can be opened,
and `_git_identity` uses the same runner to write a repository's own config.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)



# `gh auth status` prints a block per host. These pull the four facts worth
# showing out of it, so the panel renders fields a person can read rather than
# the raw block — which arrives as one long line and says "keyring" and
# "Token scopes" to someone who never asked about either.
_GH_ACCOUNT = re.compile(r"Logged in to \S+ account (\S+)")
_GH_PROTOCOL = re.compile(r"Git operations protocol:\s*(\S+)")
_GH_SCOPES = re.compile(r"Token scopes:\s*(.+)")


def _parse_gh_status(text: str) -> dict[str, Any]:
    account = _GH_ACCOUNT.search(text)
    protocol = _GH_PROTOCOL.search(text)
    scopes = _GH_SCOPES.search(text)
    return {
        "account": account.group(1) if account else None,
        "protocol": protocol.group(1) if protocol else None,
        "scopes": (
            [s.strip().strip("'\"") for s in scopes.group(1).split(",") if s.strip()]
            if scopes
            else []
        ),
    }


async def probe(argv: list[str], timeout: float | None = None) -> tuple[int, str]:
    """Run one read-only command. Never raises: a machine without the program
    is a normal machine, and 127 is the answer to "is it installed".

    The deadline comes from `mirror.yaml`, like the one the root probe below
    uses. A number written here would be the one thing on this page nobody
    could tune from the file that claims to own it.
    """
    if timeout is None:
        timeout = _probe_timeout_s()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        return 127, ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 1, ""
    return proc.returncode or 0, out.decode("utf-8", "replace").strip()


#: What `git --version` and `gh auth status` last answered, or `None`.
#:
#: These two are facts about the MACHINE, and the machine does not change while
#: the panel is open. `gh auth status` alone costs about 0.66s because it opens
#: the Windows keyring, and it was being paid on every open of a panel whose
#: other answers are nearly free. Splitting "facts about this machine" from
#: "facts about this project" is the same split the identity record needs,
#: which is why it lands here rather than as a timer.
#:
#: **It never expires on its own.** A cache with a lifetime says "signed in"
#: for another minute after a sign-in has gone, which is the class of lie the
#: registry panel spent four audit passes removing. It is dropped by the one
#: event in this process that changes the answer, and re-read on demand when
#: the operator asks the panel to check again.
_MACHINE_PROBES: tuple[tuple[int, str], tuple[int, str]] | None = None

#: The probe currently in flight, shared by everyone waiting on it. Two panels
#: opening together used to run `gh auth status` twice and throw one answer
#: away, which is the cost this cache exists to remove, paid at the one moment
#: the cache cannot help.
_MACHINE_INFLIGHT: asyncio.Future[tuple[tuple[int, str], tuple[int, str]]] | None = None


def invalidate_machine_probes() -> None:
    """Forget what this machine last said about git. Called by connect and
    disconnect, which are the only things here that change it."""
    global _MACHINE_PROBES
    _MACHINE_PROBES = None


async def _machine_probes(*, fresh: bool) -> tuple[tuple[int, str], tuple[int, str]]:
    """`(git --version, gh auth status)`, cached unless `fresh`.

    A probe that RAISED is not cached. An exception is "this did not answer",
    not an answer, and remembering it would turn one glitch into a panel
    reporting git missing until something else invalidated.

    Callers arriving while one is running share it. That includes a `fresh`
    caller, because a probe that started after they asked is already the
    answer they wanted, and starting a second is spending 0.66s to learn the
    same thing.
    """
    global _MACHINE_INFLIGHT
    if not fresh and _MACHINE_PROBES is not None:
        return _MACHINE_PROBES
    if _MACHINE_INFLIGHT is None:
        # A TASK, not this coroutine's own work, and every caller then waits
        # on it shielded. The probe belongs to nobody in particular: a client
        # that closes its tab mid-request must not cancel the answer the other
        # waiters are still holding out for, and the request that happened to
        # arrive first is not more entitled to be the owner than the rest.
        _MACHINE_INFLIGHT = asyncio.ensure_future(_run_machine_probes())
        _MACHINE_INFLIGHT.add_done_callback(_release_machine_probe)
    return await asyncio.shield(_MACHINE_INFLIGHT)


def _release_machine_probe(task: asyncio.Future[Any]) -> None:
    """Let the next caller start a new probe, and read any failure.

    An exception nobody retrieves is reported by the loop at collection time
    as an error with no context and nothing to act on, so it is read here even
    though the waiters have already had it.
    """
    global _MACHINE_INFLIGHT
    _MACHINE_INFLIGHT = None
    if not task.cancelled():
        task.exception()


async def _run_machine_probes() -> tuple[tuple[int, str], tuple[int, str]]:
    """The two probes, and the decision about whether to remember them."""
    global _MACHINE_PROBES

    results = await asyncio.gather(
        probe(["git", "--version"]),
        probe(["gh", "auth", "status"]),
        return_exceptions=True,
    )
    answers: list[tuple[int, str]] = []
    complete = True
    for index, value in enumerate(results):
        if isinstance(value, BaseException):
            log.info("get_git: machine probe %d failed (%s)", index, value)
            answers.append((1, ""))
            complete = False
        else:
            answers.append(value)
    probes = (answers[0], answers[1])
    if complete:
        _MACHINE_PROBES = probes
    return probes


def _probe_timeout_s() -> float:
    """How long to wait for `git` or `gh` to answer, from `mirror.yaml`."""
    return _probes_value("command_timeout_s")


#: `(mtime, value)`. The deadline is read from disk, and re-reading it per
#: probe put a synchronous parse on the event loop on every panel poll. A stat
#: is far cheaper than a read plus a parse, and comparing it still lets an edit
#: land without a restart.
_TIMEOUT_CACHE: dict[str, tuple[float, float]] = {}


def _root_stat_timeout_s() -> float:
    """The deadline for any filesystem question this module asks about a root."""
    return _probes_value("root_stat_timeout_s")


def _probes_value(key: str) -> float:
    """One number from `mirror.yaml`'s `probes` block, cached on the file's
    mtime.

    Loud on a missing key per the config rule: a probe that silently fell back
    to a built-in default would be the one thing nobody could tune from the
    file that claims to own it. A stat is far cheaper than a read plus a
    parse, and comparing it still lets an edit land without a restart.
    """
    global _TIMEOUT_CACHE
    from tesseract.mirror.server.config import MIRROR_YAML, _load_yaml

    try:
        mtime = MIRROR_YAML.stat().st_mtime
    except OSError:
        mtime = -1.0
    cached = _TIMEOUT_CACHE.get(key) if _TIMEOUT_CACHE else None
    if cached is not None and cached[0] == mtime:
        return cached[1]

    probes = _load_yaml(MIRROR_YAML).get("probes")
    if not isinstance(probes, dict):
        raise RuntimeError(f"{MIRROR_YAML} missing required 'probes' block")
    try:
        value = float(probes[key])
    except KeyError as exc:
        raise RuntimeError(f"{MIRROR_YAML} probes.* missing key: {exc.args[0]}") from exc
    _TIMEOUT_CACHE[key] = (mtime, value)
    return value


# ─── The root probe ──────────────────────────────────────────────────
#
# **Every property this must hold AT ONCE.** A change that satisfies one of
# these and quietly drops another is the failure this list exists to stop; it
# cost three rewrites before anyone wrote it down. Check a change against all
# five, not against the one that prompted it.
#
#   1. The caller returns inside the deadline, whatever the filesystem does.
#   2. At most one live thread per distinct question, however often it is asked.
#   3. An abandoned probe dies with the process; shutdown is never held open.
#   4. A read degrades to "nothing is known"; a writer refuses.
#   5. No blocking filesystem call ever runs inside the registry's write lock.
#
# Why each is fragile: (1) `Path.is_dir()` on an unmounted drive blocks for as
# long as the OS takes, and cannot be cancelled, so the deadline is what
# answers. (3) rules out a pooled executor, because
# `concurrent.futures.thread._python_exit` joins every live worker in
# `_threads_queues` at interpreter exit and asyncio's default executor is in
# that same registry. (2) is what a bare thread-per-call drops, and a cap is
# the wrong answer to it: refusing the fifth caller invents a failure when the
# honest reply is the answer the first four are already waiting for.

#: One in-flight probe per question. The key is the QUESTION, never just the
#: path: `_roots_alive` asks whether a directory is there, `unopenable` asks
#: that AND whether it is sealed, and one answer must never serve both.
_IN_FLIGHT: dict[Any, asyncio.Future[Any]] = {}


def _spawn_probe(loop: Any, answer: asyncio.Future[Any], fn: Any, args: tuple) -> None:
    """Run `fn` on a daemon thread and settle `answer` with what it returns."""

    def _settle(setter: Any, value: Any) -> None:
        if not answer.done():
            setter(value)

    def _work() -> None:
        try:
            result = fn(*args)
        except BaseException as exc:  # noqa: BLE001 — carried to the awaiter
            settle = (answer.set_exception, exc)
        else:
            settle = (answer.set_result, result)
        try:
            loop.call_soon_threadsafe(_settle, *settle)
        except RuntimeError:
            # The loop closed while this probe was still blocked, which is the
            # abandoned case working as intended. There is nobody left to tell,
            # and raising here would surface as an unhandled exception in a
            # bare thread while the process is already on its way down.
            pass

    threading.Thread(target=_work, daemon=True, name="root-stat").start()


async def under_stat_deadline(key: Any, fn: Any, *args: Any, default: Any) -> Any:
    """`fn(*args)` on a daemon thread under the deadline, else `default`.

    Callers asking the same question share one probe. `asyncio.shield` is what
    makes that safe: `wait_for` cancels what it is waiting on, and without the
    shield the first caller to time out would cancel the answer every other
    caller is still waiting for.

    Sharing an in-flight probe is not staleness. While one is outstanding
    nobody can know anything fresher than "still unknown", so the shared result
    is exactly what serialising the callers would have produced. Caching a
    FINISHED answer would be staleness, and is deliberately not done: a root
    that comes back would keep reading dead until the entry expired.
    """
    loop = asyncio.get_running_loop()
    shared = _IN_FLIGHT.get(key)
    if shared is None:
        shared = loop.create_future()
        _IN_FLIGHT[key] = shared
        shared.add_done_callback(lambda _f: _IN_FLIGHT.pop(key, None))
        _spawn_probe(loop, shared, fn, args)
    try:
        return await asyncio.wait_for(
            asyncio.shield(shared), timeout=_root_stat_timeout_s()
        )
    except asyncio.TimeoutError:
        log.info("settings: a root did not answer within the stat deadline")
        return default


def _roots_alive(roots: list[str]) -> dict[str, bool]:
    """Whether each registered root is still a directory on this machine.

    Runs off the event loop, under a deadline the caller sets. A root on an
    unmounted network drive blocks its stat for as long as the OS takes to give
    up, and every other panel shares this loop.

    A root that cannot be stat'ed at all is reported missing rather than
    raising: the panel's job is to say what it found, and one unreadable path
    must not cost the operator the rest of the table.
    """
    alive: dict[str, bool] = {}
    for root in roots:
        try:
            alive[root] = Path(root).is_dir()
        except OSError:
            alive[root] = False
    return alive


async def _roots_alive_bounded(roots: list[str]) -> dict[str, bool]:
    """`_roots_alive` under the deadline, degrading to "nothing is known".

    An empty map is what the caller reads as "every root present", so a
    timeout costs the missing-folder mark and never invents one. The panel is
    a report: the safe direction for it is to say less, not to guess.
    """
    # Keyed on the roster, so a panel polling an unchanged list of roots
    # while one of them is dead shares a single probe however often it asks.
    return await under_stat_deadline(
        ("roots", tuple(sorted(roots))), _roots_alive, roots, default={}
    )


def unopenable(root: str | Path) -> str | None:
    """Why `root` cannot be a working directory, or ``None`` when it can.

    The two questions `project_open` and `project_link` already ask before they
    let a root through (`project_open.py:102`, `project_link.py:109`), asked
    here for the same reason they ask them: the seal is re-checked at the lane
    that actually spawns (`lanes/manager.py:274`), so skipping it here does not
    let anything run in `app/` — it just moves the refusal to a lane open,
    minutes away from the click that chose the root, where it reads as a broken
    project rather than as an answer to what was asked.
    """
    from tesseract.orchestrator.seal_guard import SealViolation, assert_cwd_outside_seal

    try:
        if not Path(root).is_dir():
            return f"{root} is not a folder on this machine"
    except OSError as exc:
        return f"{root} cannot be read: {exc}"
    try:
        assert_cwd_outside_seal(root)
    except SealViolation as exc:
        return str(exc)
    return None
