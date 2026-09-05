"""ProviderProbeJob — known-good probe across every active role.

**Two entry points, one at a time.** It is a stage of the `consolidate` row —
the nightly backstop — AND a row on `when: provider_failover`, which fires when
real traffic has already fallen back from a primary. Both drive this same class,
and the writer they share states a single-writer contract:
`provider_health.py::_rotate_if_needed` warns that two callers can both observe
an oversize file and race on `rename`, dropping a row on Windows or overwriting
the archive on POSIX. `_ONE_AT_A_TIME` below is how that contract is kept now
that a second caller exists — the failover the trigger reacts to can happen at
any hour, including while the nightly pass is running.

It calls each REF directly rather than riding a chain, which is why the row's
manifest entry names `*` alongside the chain its other stages ride.

**One probe per distinct ref.** The unit of health is the ref, not the role:
a model either answers or it does not, and which roles name it changes
nothing. Every active role's primary AND fallbacks (plus the top-level
``embeddings`` ref) are collapsed to a roster of distinct refs, each probed
once, and the roles that named it ride along on the row so the fan-out stays
readable. Five of last night's thirteen calls were the same question asked
again.

The probe for a ref is chosen by TIER first, then by the model's ``kind``
(``chat`` / ``embedding`` / ``image_generation``). A ``cli``-tier ref is a
subscription, so it is asked the two questions a subscription can fail at
separately — signed in, and still answering — once per provider rather than
once per ref. See ``_probes/cli_role.py``. The result row is written via
:func:`tesseract.orchestrator.provider_health.record_probe_result`.

On any ``ok=False`` row, the job ALSO publishes a ``provider_health``
event to the AgendaStore bus via the module-level publisher in
:mod:`tesseract.orchestrator.autonomy.publishers`. The
``provider_watch`` mapper consumes those events; here we only emit.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Any

from tesseract.config.loader import ConfigBundle, load_config
from tesseract.orchestrator.autonomy.models import AgendaSource
from tesseract.orchestrator.autonomy.publishers import publish_to_bus
from tesseract.orchestrator import provider_failure
from tesseract.orchestrator.provider_health import record_probe_result
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.tasks._probes.base import ProbeResult, RoleProbe
from tesseract.scheduler.tasks._probes.chat_role import ChatRoleProbe
from tesseract.scheduler.tasks._probes.cli_role import CliProbe
from tesseract.scheduler.tasks._probes.embedding_role import EmbeddingRoleProbe
from tesseract.scheduler.tasks._probes.image_role import ImageRoleProbe
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


# Serialises the nightly stage against the failover row. Module-level and
# asyncio-scoped because both callers run in the one Mirror event loop: the
# scheduler dispatches every row and every stage there, so a lock here is the
# whole guard. A second process would need the file lock the writer's docstring
# names as the alternative; nothing runs this out of process today.
_ONE_AT_A_TIME = asyncio.Lock()


class ProviderProbeJob(BaseJob):
    uses_llm = False  # adapter is invoked via probe, not by the scheduler harness

    async def run(self, ctx: JobContext) -> JobResult:
        async with _ONE_AT_A_TIME:
            return await self._run(ctx)

    async def _run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            bundle = _load_bundle_safely()
        except Exception as exc:  # noqa: BLE001
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"load_config failed: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )

        targets = _collect_probe_targets(bundle)
        if not targets:
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail="no active refs to probe",
                payload={"probed": 0, "skipped": [], "failures": [], "ok": True},
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )

        probes_by_kind = _build_probes(ctx, bundle)
        probes_by_provider = await _build_cli_probes(bundle, targets, ctx.trigger_source)
        publisher = _select_publisher(ctx)

        results: list[ProbeResult] = []
        skipped: list[dict[str, Any]] = []
        for target in targets:
            role_name, ref = target.roles[0], target.ref
            probe = (
                probes_by_provider.get(target.provider)
                if target.tier == "cli"
                else probes_by_kind.get(target.kind)
            )
            if probe is None:
                skipped.append(
                    {"ref": ref, "kind": target.kind, "roles": list(target.roles)}
                )
                continue
            try:
                result = await probe.probe(role_name, ref)
            except Exception as exc:  # noqa: BLE001
                log.exception("provider_probe: %s probe crashed", ref)
                # Stamp ``probed_at`` with the current UTC ISO timestamp
                # so ``provider_health.rolling_window`` (which parses this
                # field) keeps the row visible to the mapper. An
                # empty string would parse-fail and silently drop the
                # crash signal — the *worst* signal to lose.
                result = ProbeResult(
                    role=role_name,
                    ref=ref,
                    ok=False,
                    drift_kind="http_error",
                    # OURS: the probe itself died, so nothing about this
                    # provider was learned. Every probe keeps this
                    # distinction, whichever tier it serves.
                    evidence=provider_failure.evidence(
                        provider_failure.ours(f"the probe crashed ({exc!r})")
                    ),
                    probed_at=datetime.now(timezone.utc).isoformat(),
                    latency_ms=0.0,
                )
            # The row is keyed by ref; the roles that named it are what makes
            # a report say "these two seats go quiet together" without asking
            # roles.yaml a second time.
            result = replace(result, extra={
                **result.extra,
                "roles": list(target.roles),
                "leads": list(target.leads),
            })
            record_probe_result(result, publisher=publisher)
            results.append(result)

        # The ATTRIBUTION, not the sentence — the same cut the bus publisher
        # makes, for the same reason. `asdict` carries `evidence`, which now
        # holds up to 400 characters of a CLI's own stdout and stderr, and
        # this payload is written verbatim into `runs.jsonl`, which the
        # watchman reads and the Schedule surface renders. The sentence stays
        # in the health log, where SECURITY.md says it stays.
        failures = [_failure_row(r) for r in results if not r.ok]
        # JobResult.ok is unconditionally True when probes ran. Drift in
        # a single probe is *expected output* — the job reached every
        # role and recorded telemetry. If JobResult.ok were False the
        # scheduler would retry, masking the drift signal the mapper
        # needs to see. payload["ok"] (below) carries the
        # "everything healthy?" boolean for dashboards.
        return JobResult(
            job_name=ctx.job_name,
            run_id=ctx.run_id,
            ok=True,
            detail=(
                f"probed {len(results)} ref(s); {len(failures)} drift"
                if results
                else "no probes ran"
            ),
            payload={
                "probed": len(results),
                "skipped": skipped,
                "failures": failures,
                "ok": not failures,
            },
            duration_ms=(time.monotonic() - t0) * 1000.0,
        )


def _failure_row(result: ProbeResult) -> dict[str, Any]:
    """One failed probe, for the run record: what failed and whose fault."""
    row = asdict(result)
    evidence = row.pop("evidence", None) or {}
    row["origin"] = evidence.get("origin", "")
    row["fault_kind"] = evidence.get("kind", "")
    return row


def _load_bundle_safely() -> ConfigBundle:
    return load_config()


@dataclass(frozen=True)
class ProbeTarget:
    """One distinct ref to probe, and every role that named it.

    ``roles`` is ordered as the catalog was walked, and its first entry is the
    role the probe runs under — the billing key the ledger records, which has
    to be one name however many roles share the ref.
    """

    ref: str
    kind: str
    tier: str
    provider: str
    roles: tuple[str, ...]
    #: The roles this ref is the FIRST choice of, which is a different list
    #: from `roles` and the difference is what a reader needs. A ref nothing
    #: leads with is a spare: it failing costs nothing yet, because every role
    #: that names it is still answering on the entry above it. A ref that
    #: leads is what a role reaches for first, and it failing IS a
    #: degradation. Grading both the same is what made a slow spare read as
    #: an emergency (operator, 2026-09-02).
    leads: tuple[str, ...] = ()
    # `cli` tier only — the provider's `live_check` block, carried here
    # because this is where the connection was already in hand.
    live_check: Any = None


def _collect_probe_targets(bundle: ConfigBundle) -> list[ProbeTarget]:
    """Every reachable ref exactly once, with the roles that name it.

    Skips inactive, unresolved, or disabled-provider refs so the roster
    matches what is actually reachable. Deduplication is the point: probing
    per role asked `cli.codex.gpt56_terra` twice on one night and reported one
    outage as two defects.

    Fallbacks are on the roster, not just primaries. A fallback is precisely
    the entry that has to answer when the primary does not, so the first thing
    that learned one was dead used to be the failover that needed it. Three
    refs on this machine had no health history at all under the old roster.
    `boot.py::ollama_slots` walks primaries and fallbacks alike for the same
    reason, and this was the one place that stopped at the primary.
    """
    order: list[str] = []
    seen: dict[str, dict[str, Any]] = {}

    def _add(role_name: str, resolved: Any, *, leads: bool) -> None:
        conn = resolved.connection
        if not conn.tier_enabled or not conn.enabled:
            return
        entry = seen.get(resolved.ref)
        if entry is None:
            seen[resolved.ref] = {
                "kind": resolved.model.kind,
                "tier": conn.tier,
                "provider": conn.name,
                "roles": [role_name],
                "leads": [role_name] if leads else [],
                "live_check": getattr(conn, "live_check", None),
            }
            order.append(resolved.ref)
            return
        if role_name not in entry["roles"]:
            entry["roles"].append(role_name)
        # A ref can lead one role and be the spare of another, and the same
        # ref is one row. It counts as leading if it leads ANYTHING.
        if leads and role_name not in entry["leads"]:
            entry["leads"].append(role_name)

    for role_name, role in bundle.roles.items():
        if role.mode != "active" or role.primary is None:
            continue
        _add(role_name, role.primary, leads=True)
        for resolved in role.fallbacks:
            _add(role_name, resolved, leads=False)
    # Also probe the embeddings ref — it's wired outside the ``roles``
    # block (see roles.yaml ``embeddings:`` top-level).
    if bundle.embeddings is not None:
        # It is wired alone, so it is what embeddings reaches for first.
        _add("embeddings", bundle.embeddings, leads=True)

    return [
        ProbeTarget(
            ref=ref,
            kind=str(seen[ref]["kind"]),
            tier=str(seen[ref]["tier"]),
            provider=str(seen[ref]["provider"]),
            roles=tuple(seen[ref]["roles"]),
            leads=tuple(seen[ref]["leads"]),
            live_check=seen[ref]["live_check"],
        )
        for ref in order
    ]


def _build_probes(
    ctx: JobContext, bundle: ConfigBundle
) -> dict[str, RoleProbe]:
    """Construct the per-kind probe instances live-wired for ``ctx``.

    For embedding probes the orchestrator needs a callable
    ``embed_fn``. It comes from one of:

      * ``ctx.app["embedding_index"]`` (Mirror lifecycle wiring).
      * A fresh, built-from-bundle EmbeddingIndex (live runs without
        ``app``).
      * ``None`` — embedding probes are skipped (the orchestrator
        leaves the slot empty so the dispatch loop logs a skip).
    """
    probes: dict[str, RoleProbe] = {}
    probes["chat"] = ChatRoleProbe(cost_ledger=ctx.cost_ledger)
    probes["image_generation"] = ImageRoleProbe()
    embed_fn = _resolve_embed_fn(ctx, bundle)
    if embed_fn is not None:
        probes["embedding"] = EmbeddingRoleProbe(embed_fn=embed_fn)
    return probes


# The live check spends the subscription it measures, so it belongs to the
# passes that were planned. `event` is the failover row, which fires DURING an
# incident and can fire repeatedly; billing a subscription call per incident is
# the opposite of what a health check should cost when things are going wrong.
# `operator` and `assistant` are someone asking on purpose, and they get the
# real answer.
_LIVE_CHECK_TRIGGERS = frozenset({"scheduled", "catchup", "operator", "assistant", "alarm"})


async def _build_cli_probes(
    bundle: ConfigBundle, targets: list[ProbeTarget], trigger_source: str = "scheduled"
) -> dict[str, RoleProbe]:
    """One probe per `cli` provider on the roster, over a freshly-run cache.

    The refresh executes each provider's declared `auth_check` binary now,
    so the answer is about this moment rather than about boot. Running it once
    per PROVIDER rather than once per ref is why two claude refs cost one
    subprocess, and one `CliProbe` instance per provider is what holds the
    live call to the same budget — see its own docstring. It also leaves the
    cache the capabilities route reads warm, so the operator's Settings pane
    and the nightly probe stop disagreeing about the same subscription.

    Returns an empty map when nothing on the roster is `cli`, so a machine
    with no subscription wired never shells out.
    """
    providers = {t.provider for t in targets if t.tier == "cli"}
    if not providers:
        return {}
    from tesseract.brain import cli_auth

    try:
        await cli_auth.refresh(bundle)
        checked = cli_auth.auth_checked_providers(bundle)
    except Exception:  # noqa: BLE001
        log.exception("provider_probe: cli auth refresh failed")
        return {}
    spends = trigger_source in _LIVE_CHECK_TRIGGERS
    if not spends:
        log.info(
            "provider_probe: %r run — the free auth check only, no live call",
            trigger_source,
        )
    live = {t.provider: t.live_check for t in targets if t.tier == "cli"}
    return {
        name: CliProbe(
            provider=name,
            state_fn=cli_auth.get,
            live_check=live.get(name) if spends else None,
        )
        for name in providers
        if name in checked
    }


def _resolve_embed_fn(ctx: JobContext, bundle: ConfigBundle) -> Any:
    """Return ``async embed_fn(text)->vec`` or ``None`` if unwired."""
    app = ctx.app
    if app is not None and hasattr(app, "get"):
        embedding_index = app.get("embedding_index")
        if embedding_index is not None and hasattr(embedding_index, "embed_text"):
            return embedding_index.embed_text
    # Live run without ``app`` — try a fresh build from bundle. Tests
    # never hit this branch because they always inject an ``embed_fn``
    # directly into ``EmbeddingRoleProbe``.
    try:
        from tesseract.memory.embeddings import EmbeddingIndex
        from tesseract.paths import TESSERACT_HOME

        emb = bundle.embeddings
        if emb is None:
            return None
        conn = emb.connection
        if not conn.tier_enabled or not conn.enabled:
            return None
        base_url = conn.base_url or ""
        if not base_url:
            return None
        derived_dir = TESSERACT_HOME / "memory-store" / "derived"
        index = EmbeddingIndex(
            derived_dir=derived_dir,
            provider=conn.name,
            base_url=base_url,
            model=emb.model.model,
            dimensions=int(emb.model.fields.get("dimensions") or 0),
            timeout_seconds=conn.timeout_seconds,
            max_retries=conn.max_retries,
        )
        return index.embed_text
    except Exception:  # noqa: BLE001
        log.exception("provider_probe: failed to build embedding probe wiring")
        return None


def _select_publisher(ctx: JobContext) -> Any:
    """Return the bus-publish callable for drift rows.

    Tests can inject an explicit publisher through
    ``ctx.config["publisher"]``; production runs use the kernel's
    module-level :func:`publish_to_bus` so the orchestrator doesn't
    import the AutonomyKernel directly.
    """
    if isinstance(ctx.config, dict) and "publisher" in ctx.config:
        return ctx.config["publisher"]

    def _default_publisher(result: ProbeResult) -> None:
        publish_to_bus(
            AgendaSource.PROVIDER_WATCH,
            {
                "kind": "provider_health",
                "role": result.role,
                "ref": result.ref,
                "drift_kind": result.drift_kind,
                # The ATTRIBUTION, not the provider's sentence. `evidence` now
                # carries up to 400 characters of a CLI's own stdout and
                # stderr, and this payload leaves the probe for a substrate
                # whose consumers are not fixed. The sentence stays in
                # `runtime/logs/provider-health/`, where SECURITY.md says it
                # stays; what travels is two words from a closed vocabulary.
                "origin": result.evidence.get("origin", ""),
                "fault_kind": result.evidence.get("kind", ""),
                "source": result.source,
                "probed_at": result.probed_at,
            },
        )

    return _default_publisher


__all__ = ["ProviderProbeJob"]


# ── Direct-invoke entry point ───────────────────────────────────────
# ``python -m tesseract.scheduler.tasks.provider_probe`` writes a row to
# each role's JSONL. The harness below mints a minimal JobContext and
# runs the orchestrator once.


def _main() -> int:
    import uuid

    from dotenv import load_dotenv

    from tesseract.env_file import INTERPOLATE
    from tesseract.paths import home_dir

    # Every other entry point loads the credential file at boot. Without
    # it each `api.` ref answers `check_failed: <KEY> missing from .env`
    # and lands that row on disk as if it were a real result.
    load_dotenv(home_dir() / ".env", interpolate=INTERPOLATE)

    ctx = JobContext(
        job_name="provider_probe-cli",
        run_id=uuid.uuid4().hex,
        fired_at=datetime.now(timezone.utc),
    )
    job = ProviderProbeJob()
    result = asyncio.run(job.run(ctx))
    print(f"{result.detail} (duration_ms={result.duration_ms:.1f})")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
