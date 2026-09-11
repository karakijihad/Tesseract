"""One answer to "is this machine healthy", assembled from what already knows.

Every fact here was already recorded somewhere — the hardware profile the
provisioner wrote, the probe rows `provider_health` keeps, the breaker log,
`runs.jsonl`, Ollama's own tag list. What was missing was a single call that
reads them together, so answering the question meant a session of greps
against paths that had to be discovered first.

Deliberately NOT `check_dependencies.run_doctor()`, which is a *pre-flight*
gate: it asks whether this machine could start TESSERACT (is the port free, is
ffmpeg on PATH, is there disk). Several of its checks invert once the app is
actually running — a free Mirror port means the backend is DOWN. This module
asks the runtime question instead, and reuses that module's detection
primitives rather than its verdicts.

Two contracts hold for everything below:

- **Never raises.** A check that cannot run returns `unknown` with the reason.
  A diagnosis that dies on its weakest probe is worthless precisely when the
  machine is in a bad state, which is when it is called.
- **Never phones out.** Local files and localhost only: no billable calls, no
  provider round-trips. Provider health is reported from the rows the
  scheduled probes already wrote, with their age stated, so a stale answer is
  visibly stale rather than quietly wrong.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from tesseract import http_client

#: How much of a breaker's own error text a health row carries. Long enough for
#: a provider's sentence and the page it names, short enough that a stack trace
#: cannot take the row over: a remedy nobody can find in the noise is not one.
_BREAKER_REASON_CHARS = 240

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

Status = Literal["ok", "warn", "bad", "unknown"]

# Ordering for "what is the worst thing here" — `unknown` sits below `warn`
# because a check that could not run is a smaller claim than one that ran and
# disliked what it saw.
_SEVERITY: dict[Status, int] = {"ok": 0, "unknown": 1, "warn": 2, "bad": 3}


@dataclass(frozen=True)
class Check:
    """One question, its answer, and the evidence behind it."""

    name: str
    status: Status
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Diagnosis:
    generated_at: str
    checks: tuple[Check, ...]

    @property
    def worst(self) -> Status:
        return max((c.status for c in self.checks), key=lambda s: _SEVERITY[s], default="unknown")

    def by_status(self, status: Status) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.status == status)


def _unknown(name: str, exc: BaseException) -> Check:
    return Check(
        name=name,
        status="unknown",
        detail=f"check could not run: {type(exc).__name__}: {exc}",
    )


# --------------------------------------------------------------------------
# Individual checks. Each returns a list so one probe can report several
# facts, and each is responsible for its own failure mode.
# --------------------------------------------------------------------------


def _check_machine() -> list[Check]:
    from tesseract.scripts.check_dependencies import _detect_gpu, _detect_ram_gb

    gpu = _detect_gpu()
    vram = f"{gpu.memory_mb} MB" if gpu.memory_mb else "unknown VRAM"
    if gpu.vendor == "unknown":
        gpu_status: Status = "warn"
        gpu_detail = "no GPU detected — the CPU path is the only one available"
    elif gpu.cuda:
        gpu_status = "ok"
        gpu_detail = f"{gpu.name or gpu.vendor}, {vram}, CUDA reported present"
    else:
        gpu_status = "warn"
        gpu_detail = f"{gpu.name or gpu.vendor}, {vram}, CUDA not reported"

    ram = _detect_ram_gb()
    return [
        Check(
            name="gpu",
            status=gpu_status,
            detail=gpu_detail,
            evidence={"vendor": gpu.vendor, "name": gpu.name, "memory_mb": gpu.memory_mb, "cuda": gpu.cuda},
        ),
        Check(
            name="ram",
            status="ok" if (ram or 0) >= 4 else "warn",
            detail=f"{ram} GB total" if ram else "unknown (psutil missing?)",
            evidence={"ram_total_gb": ram},
        ),
    ]


def _check_acceleration() -> list[Check]:
    """What the GPU path is actually able to do, as opposed to installed.

    Reported without constructing an inference session. Building one costs
    seconds and holds the GIL through construction — the exact cost P1 spent
    a phase removing from the boot path — so a diagnosis tool must not pay it.
    Consequence, stated in the output: this reports capability and
    configuration, not the provider a live session ended up with.
    """
    from tesseract.scripts.provision_hardware import gpu_packages_ready, recorded_profile

    checks: list[Check] = []

    profile = recorded_profile()
    checks.append(
        Check(
            name="hardware_profile",
            status="ok" if profile else "unknown",
            detail=(
                f"this machine resolved to the {profile!r} profile"
                if profile
                else (
                    "never profiled, which is expected outside an installed "
                    "app: profiling runs once during first-run provisioning "
                    "and picks the speech model this machine should fetch. "
                    "Nothing is broken by its absence here; the GPU package "
                    "checks below answer for themselves"
                )
            ),
            evidence={"profile": profile},
        )
    )

    # `gpu_packages_ready` is the right question and the only one that answers
    # it honestly. Probing the pieces by hand gets two false readings:
    # `ort.get_available_providers()` lists CUDA whenever the provider DLL is
    # on disk, even when it cannot load one of its dependencies; and
    # CTranslate2's cuBLAS probe only succeeds AFTER the CUDA wheel
    # directories are added to the DLL search path, which that function does
    # first. Calling the probe cold reported "Whisper will decode on CPU int8"
    # on an install whose own profile recorded `gpu_packages_ready: true`.
    for name, extra, engine in (
        ("whisper_cuda", "gpu", "Whisper will decode on CPU int8"),
        ("kokoro_cuda", "voice-local", "Kokoro will synthesise on CPU"),
    ):
        try:
            ready = gpu_packages_ready([extra])
        except Exception as e:  # never raise — this is a diagnosis
            checks.append(_unknown(name, e))
            continue
        checks.append(
            Check(
                name=name,
                status="ok" if ready else "warn",
                detail=(
                    f"the [{extra}] GPU packages load — the engine can use the GPU"
                    if ready
                    else f"the [{extra}] GPU packages do not load — {engine}"
                ),
                evidence={"extra": extra, "packages_load": ready},
            )
        )
    return checks


async def _check_ollama() -> list[Check]:
    """Reachable, and which models are REALLY there.

    The distinction this check exists to preserve: a fetch that failed and a
    fetch that succeeded and found nothing are different machine states with
    different remedies, and collapsing them is what told the operator for two
    days that a model they had installed was missing.

    This issues its own request rather than reusing `ollama_boot.fetch_tags`,
    which now draws the same distinction via `TagFetch`. Kept separate
    deliberately: this check refuses to contact a non-loopback host (see
    below), and that contract belongs to the unattended tool rather than to
    the shared helper.
    """
    import httpx

    from tesseract.brain.boot import load_embeddings_cfg

    cfg = load_embeddings_cfg()
    base_url = cfg.get("base_url")
    wanted = cfg.get("model")
    if not base_url:
        return [
            Check(
                name="ollama",
                status="unknown",
                detail="no Ollama connection configured for the embeddings role",
            )
        ]

    # The contract that makes this tool safe to call unattended is that it
    # reaches nothing but this machine. `base_url` does not honour that on its
    # own: `providers.yaml:491` resolves it from `${OLLAMA_BASE_URL:-...}`, so
    # an environment variable could point an `auto`-posture tool at any host
    # and have it issue a request with no operator prompt. The contract wins
    # over the check — a non-loopback host is reported, not contacted.
    host = (urlparse(str(base_url)).hostname or "").lower()
    if host not in _LOOPBACK_HOSTS:
        return [
            Check(
                name="ollama",
                status="unknown",
                detail=(
                    f"configured at {base_url}, which is not loopback — not contacted. "
                    "This check is local-only by contract, so a remote Ollama is "
                    "reported rather than probed."
                ),
                evidence={"base_url": base_url, "host": host},
            )
        ]

    url = f"{str(base_url).rstrip('/')}/api/tags"
    try:
        async with http_client.async_client(timeout=5.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as e:
        return [
            Check(
                name="ollama",
                status="bad",
                detail=(
                    f"tag fetch FAILED against {url} ({type(e).__name__}). "
                    "This says nothing about which models are installed — "
                    "the daemon did not answer. Remedy is to start or reach "
                    "Ollama, not to pull anything."
                ),
                evidence={"base_url": base_url, "error": type(e).__name__},
            )
        ]
    except ValueError as e:
        return [
            Check(
                name="ollama",
                status="bad",
                detail=f"tag fetch returned unparseable JSON from {url}: {e}",
                evidence={"base_url": base_url},
            )
        ]

    models = [m.get("name", "") for m in (payload.get("models") or []) if isinstance(m, dict)]
    checks = [
        Check(
            name="ollama",
            status="ok",
            detail=f"reachable at {base_url}, {len(models)} model(s) installed",
            evidence={"base_url": base_url, "models": models},
        )
    ]
    if wanted:
        target = str(wanted).split(":", 1)[0]
        present = any(tag == wanted or tag.split(":", 1)[0] == target for tag in models)
        checks.append(
            Check(
                name="ollama_embedding_model",
                status="ok" if present else "bad",
                detail=(
                    f"{wanted} is installed"
                    if present
                    else f"{wanted} is genuinely absent — the fetch succeeded and did not list it. Remedy: ollama pull {wanted}"
                ),
                evidence={"wanted": wanted, "installed": models},
            )
        )
    return checks


def _check_breakers() -> list[Check]:
    from tesseract.context.circuit_breaker import (
        load_tripped_breakers,
        tripped_breaker_reasons,
    )
    from tesseract.paths import log_dir

    root = log_dir("circuit-breakers")
    tripped = load_tripped_breakers(root)
    open_names = sorted(name for name, is_open in tripped.items() if is_open)
    # The provider's own sentence, carried through instead of dropped. A name
    # says something is shut; only this says whether to wait, to fix a config,
    # or to go and pay a bill. Bounded because a stack trace in a health row is
    # not a remedy either.
    reasons = tripped_breaker_reasons(root)
    said = {n: reasons.get(n, "")[:_BREAKER_REASON_CHARS] for n in open_names}
    if open_names:
        detail = f"{len(open_names)} breaker(s) open: " + "; ".join(
            f"{n} ({said[n]})" if said[n] else n for n in open_names
        )
    else:
        detail = f"no breakers open ({len(tripped)} tracked)"
    return [
        Check(
            name="circuit_breakers",
            status="bad" if open_names else "ok",
            detail=detail,
            evidence={
                "open": open_names,
                "tracked": sorted(tripped),
                "why": said,
            },
        )
    ]


def _check_providers() -> list[Check]:
    """Last recorded probe per ref, with its age stated.

    Age is the load-bearing part. A ref whose last probe was green four days
    ago is not a healthy ref, and reporting the verdict without the age is
    how a stale record becomes a current claim.
    """
    from tesseract.orchestrator.provider_health import iter_refs_with_history, tail_recent

    now = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    for ref in iter_refs_with_history():
        recent = tail_recent(ref, n=1)
        if not recent:
            continue
        row = recent[-1]
        age_hours: float | None = None
        stamp = row.get("probed_at")
        if isinstance(stamp, str):
            try:
                probed = datetime.fromisoformat(stamp)
                if probed.tzinfo is None:
                    probed = probed.replace(tzinfo=timezone.utc)
                age_hours = round((now - probed).total_seconds() / 3600, 1)
            except ValueError:
                age_hours = None
        rows.append({"ref": ref, "ok": bool(row.get("ok")), "age_hours": age_hours})

    if not rows:
        return [
            Check(
                name="provider_health",
                status="unknown",
                detail="no probe history recorded yet for any ref",
            )
        ]

    def _label(row: dict[str, Any]) -> str:
        age = row.get("age_hours")
        return f"{row['ref']} ({age}h ago)" if age is not None else f"{row['ref']} (age unknown)"

    failing = [r for r in rows if not r["ok"]]
    ages = [r["age_hours"] for r in rows if r.get("age_hours") is not None]
    oldest = f", oldest {max(ages)}h" if ages else ""
    return [
        Check(
            name="provider_health",
            status="warn" if failing else "ok",
            detail=(
                # The age belongs in the summary, not only in the evidence. A
                # ref that was green four days ago is not a healthy ref, and
                # a verdict without its age reads as current.
                f"{len(failing)} of {len(rows)} refs failing their last probe: "
                + ", ".join(_label(r) for r in failing)
                if failing
                else f"all {len(rows)} probed refs green on their last probe{oldest}"
            ),
            evidence={"refs": rows},
        )
    ]


def _check_scheduler() -> list[Check]:
    from tesseract.paths import log_dir

    path = log_dir("schedule") / "runs.jsonl"
    if not path.exists():
        return [
            Check(
                name="scheduler",
                status="unknown",
                detail=f"no run log at {path} — the scheduler has never written one",
            )
        ]

    last_by_job: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            name = row.get("job_name")
            if isinstance(name, str):
                last_by_job[name] = row

    if not last_by_job:
        return [Check(name="scheduler", status="unknown", detail=f"{path} holds no readable rows")]

    failed = sorted(name for name, row in last_by_job.items() if not row.get("ok", True))
    return [
        Check(
            name="scheduler",
            status="warn" if failed else "ok",
            detail=(
                f"{len(failed)} of {len(last_by_job)} jobs failed their last run: {', '.join(failed)}"
                if failed
                else f"all {len(last_by_job)} jobs green on their last run"
            ),
            evidence={
                "last_run": {
                    name: {"ok": row.get("ok"), "fired_at": row.get("fired_at"), "detail": row.get("detail")}
                    for name, row in sorted(last_by_job.items())
                }
            },
        )
    ]


def _check_disk() -> list[Check]:
    from tesseract.paths import app_dir, home_dir, runtime_dir

    checks: list[Check] = []
    for label, path in (("home", home_dir()), ("app", app_dir()), ("runtime", runtime_dir())):
        if not path.exists():
            checks.append(
                Check(
                    name=f"disk_{label}",
                    status="unknown",
                    detail=f"{path} does not exist (dev checkout, or not provisioned)",
                    evidence={"path": str(path)},
                )
            )
            continue
        usage = shutil.disk_usage(path)
        free_gb = round(usage.free / 1024**3, 1)
        checks.append(
            Check(
                name=f"disk_{label}",
                status="ok" if free_gb >= 2 else "bad",
                detail=f"{free_gb} GB free on the volume holding {path}",
                evidence={"path": str(path), "free_gb": free_gb},
            )
        )
    return checks


def _check_audio() -> list[Check]:
    """Can this machine hear and speak right now.

    Not the same question as `check_dependencies.py`'s microphone count, which
    is a PRE-FLIGHT capability field: how many input devices exist says
    nothing about whether the engines behind them are on disk. Capture itself
    happens in the browser, so the runtime's half of the audio path is the two
    voice lanes and the wake gate, and those are exactly the parts that fail
    quietly.

    Cheap by construction — resolved config and a handful of `is_file` calls.
    No model is loaded and no audio is synthesised.
    """
    from tesseract.config.loader import load_config
    from tesseract.voice import wake_spotter

    checks: list[Check] = []
    try:
        voice = load_config().voice
    except Exception as exc:  # noqa: BLE001 — never-raise contract
        checks.append(_unknown("voice", exc))
        voice = None

    for lane in ("stt", "tts"):
        chain = getattr(voice, lane, None) if voice is not None else None
        if chain is None:
            continue
        local = [p.ref for p in chain.chain() if p.ref.connection.tier == "local"]
        if not local:
            # Every entry in the lane is a hosted provider, whose reachability
            # `provider_health` already answers. Nothing local to check.
            checks.append(
                Check(
                    name=f"voice_{lane}",
                    status="ok",
                    detail=f"the {lane} lane runs on a hosted provider, not on this machine",
                )
            )
            continue
        answers = {r.ref: _voice_files_present(r) for r in local}
        present = [ref for ref, ok in answers.items() if ok is True]
        missing = [ref for ref, ok in answers.items() if ok is False]
        # An entry whose catalog record names no file is neither present nor
        # missing, and reporting it as either would be inventing an answer.
        unknown = [ref for ref, ok in answers.items() if ok is None]
        hosted = len(chain.chain()) - len(local)
        detail = (
            f"{len(present)} of {len(local)} local {lane} engine(s) have their "
            "model files on disk"
        )
        if missing:
            detail += f". Missing: {', '.join(missing)}"
        if unknown:
            detail += f". Cannot tell for: {', '.join(unknown)}"
        checks.append(
            Check(
                name=f"voice_{lane}",
                # `warn`, not `bad`, while a hosted entry is still in the
                # chain: the lane degrades to that rather than going silent,
                # which is a different thing to tell the operator.
                # An entry nobody can answer for keeps the lane off `ok`,
                # because the detail says so in the same breath and a status
                # that disagrees with its own sentence is the header lie this
                # phase exists to remove.
                status=(
                    "bad" if missing and not hosted
                    else "warn" if missing
                    else "unknown" if unknown
                    else "ok"
                ),
                detail=detail,
                evidence={
                    "present": present, "missing": missing,
                    "unknown": unknown, "hosted_fallbacks": hosted,
                },
            )
        )

    try:
        reason = wake_spotter.unavailable_reason()
    except Exception as exc:  # noqa: BLE001 — never-raise contract
        checks.append(_unknown("wake_word", exc))
        return checks
    checks.append(
        Check(
            name="wake_word",
            # A wake gate that cannot arm passes every utterance through
            # rather than blocking, so this is a `warn`: what is lost is the
            # filtering, not the listening.
            status="ok" if reason is None else "warn",
            detail=reason or "the wake decoder can be built on this machine",
        )
    )
    return checks


def _voice_files_present(ref: Any) -> bool | None:
    """Whether one local voice ref's model files are on disk. `None` when the
    catalog entry does not name a file, so the question cannot be answered.

    Whisper keeps a snapshot directory per checkpoint, and
    `whisper_model_source` returning the bare checkpoint name is the library's
    own signal that no complete snapshot was fetched. Every other local engine
    keeps NAMED files in the lane directory: the entry's `model` is the file
    and `voices_file`, where an entry has one, is the second.

    **The files are named, not counted.** This asked whether the lane
    directory had anything in it, and both shipped lanes ship a `README.md`,
    so deleting every model file left the check answering yes. That is the
    same defect as the wake gate reporting three present files missing, in the
    other direction and in the check written to catch it.
    """
    from tesseract.voice import model_files

    provider = ref.connection.name
    if provider == "whisper":
        name = ref.model.model
        return model_files.whisper_model_source(name) != name

    directory = model_files.lane_dir(provider)
    fields = getattr(ref.model, "fields", None) or {}
    wanted = [
        str(name)
        for name in (ref.model.model, fields.get("voices_file"))
        # A `model` that is a checkpoint name rather than a filename says
        # nothing about the disk, and guessing from it is how a check starts
        # answering a question it was not asked.
        if name and "." in str(name)
    ]
    if not wanted:
        return None
    return all((directory / name).is_file() for name in wanted)


async def collect_diagnosis() -> Diagnosis:
    """Run every check concurrently and assemble the report.

    The synchronous checks go through `to_thread` because two of them are slow
    in ways that would otherwise be paid on the event loop: the GPU probe
    spawns `nvidia-smi`, and the scheduler check reads a growing JSONL. Neither
    is allowed to make the backend miss a heartbeat to answer a question about
    its own health.
    """
    tasks = [
        asyncio.to_thread(_check_machine),
        asyncio.to_thread(_check_acceleration),
        _check_ollama(),
        asyncio.to_thread(_check_breakers),
        asyncio.to_thread(_check_providers),
        asyncio.to_thread(_check_scheduler),
        asyncio.to_thread(_check_disk),
        asyncio.to_thread(_check_audio),
    ]
    names = [
        "machine", "acceleration", "ollama", "breakers", "provider_health",
        "scheduler", "disk", "audio",
    ]

    settled = await asyncio.gather(*tasks, return_exceptions=True)

    checks: list[Check] = []
    for name, outcome in zip(names, settled):
        # Cancellation is not a diagnosis result. `return_exceptions=True`
        # captures CancelledError from a child alongside real failures, and
        # folding it into an "unknown" check would report a cancelled run as a
        # completed one and stop the cancellation propagating.
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome
        if isinstance(outcome, BaseException):
            checks.append(_unknown(name, outcome))
        elif isinstance(outcome, list):
            checks.extend(outcome)
        else:
            # The never-raise contract has to hold against a check that returns
            # the wrong shape too, or the guarantee is only as good as the next
            # edit to this file.
            checks.append(
                Check(
                    name=name,
                    status="unknown",
                    detail=f"check returned {type(outcome).__name__}, expected a list of Check",
                )
            )

    return Diagnosis(generated_at=datetime.now(timezone.utc).isoformat(), checks=tuple(checks))


_MARK: dict[Status, str] = {"ok": "OK", "warn": "WARN", "bad": "BAD", "unknown": "????"}


def render_text(diagnosis: Diagnosis) -> str:
    """Human- and model-readable rendering, worst first."""
    ordered = sorted(diagnosis.checks, key=lambda c: -_SEVERITY[c.status])
    lines = [
        f"System diagnosis — worst status: {diagnosis.worst.upper()} "
        f"({len(diagnosis.by_status('bad'))} bad, {len(diagnosis.by_status('warn'))} warn, "
        f"{len(diagnosis.by_status('unknown'))} unknown, {len(diagnosis.by_status('ok'))} ok)",
        f"generated_at {diagnosis.generated_at}",
        "",
    ]
    for check in ordered:
        lines.append(f"[{_MARK[check.status]}] {check.name}: {check.detail}")
    lines.extend(
        [
            "",
            "Acceleration reflects installed capability and configuration, not the "
            "execution provider a live model session actually received — reading "
            "that would require constructing a session, which is expensive and "
            "blocking. Provider health is the last RECORDED probe, not a live call.",
        ]
    )
    return "\n".join(lines)


__all__ = ["Check", "Diagnosis", "Status", "collect_diagnosis", "render_text"]
