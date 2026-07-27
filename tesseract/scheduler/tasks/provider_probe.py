"""ProviderProbeJob — daily known-good probe across every active role.

For each role in ``roles.yaml::roles`` with ``mode: active``, the job
looks up the primary catalog ref's ``kind`` (``chat`` / ``embedding`` /
``image_generation``), dispatches the matching probe from
:mod:`tesseract.scheduler.tasks._probes`, and writes the result row via
:func:`tesseract.orchestrator.provider_health.record_probe_result`.

On any ``ok=False`` row, the job ALSO publishes a ``provider_health``
event to the AU-4 AgendaStore bus via the module-level publisher in
:mod:`tesseract.orchestrator.autonomy.publishers`. AU-5's
``provider_watch`` mapper consumes those events; here we only emit.

The job is **disabled by default** in ``schedule.yaml`` — operator opts
in once the probe roster matches their cost / cadence preferences.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from tesseract.config.loader import ConfigBundle, load_config
from tesseract.orchestrator.autonomy.models import AgendaSource
from tesseract.orchestrator.autonomy.publishers import publish_to_bus
from tesseract.orchestrator.provider_health import record_probe_result
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.tasks._probes.base import ProbeResult, RoleProbe
from tesseract.scheduler.tasks._probes.chat_role import ChatRoleProbe
from tesseract.scheduler.tasks._probes.embedding_role import EmbeddingRoleProbe
from tesseract.scheduler.tasks._probes.image_role import ImageRoleProbe
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


class ProviderProbeJob(BaseJob):
    uses_llm = False  # adapter is invoked via probe, not by the scheduler harness

    async def run(self, ctx: JobContext) -> JobResult:
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

        roles_to_probe = _collect_active_roles(bundle)
        if not roles_to_probe:
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail="no active roles to probe",
                payload={"probed": 0, "skipped": [], "failures": [], "ok": True},
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )

        probes_by_kind = _build_probes(ctx, bundle)
        publisher = _select_publisher(ctx)

        results: list[ProbeResult] = []
        skipped: list[dict[str, Any]] = []
        for role_name, ref, kind in roles_to_probe:
            probe = probes_by_kind.get(kind)
            if probe is None:
                skipped.append({"role": role_name, "ref": ref, "kind": kind})
                continue
            try:
                result = await probe.probe(role_name, ref)
            except Exception as exc:  # noqa: BLE001
                log.exception("provider_probe: %s probe crashed", role_name)
                # Stamp ``probed_at`` with the current UTC ISO timestamp
                # so ``provider_health.rolling_window`` (which parses this
                # field) keeps the row visible to the AU-5 mapper. An
                # empty string would parse-fail and silently drop the
                # crash signal — the *worst* signal to lose.
                result = ProbeResult(
                    role=role_name,
                    ref=ref,
                    ok=False,
                    drift_kind="http_error",
                    evidence={"exception": repr(exc)},
                    probed_at=datetime.now(timezone.utc).isoformat(),
                    latency_ms=0.0,
                )
            record_probe_result(result, publisher=publisher)
            results.append(result)

        failures = [asdict(r) for r in results if not r.ok]
        # JobResult.ok is unconditionally True when probes ran. Drift in
        # a single probe is *expected output* — the job reached every
        # role and recorded telemetry. If JobResult.ok were False the
        # scheduler would retry, masking the drift signal AU-5's mapper
        # needs to see. payload["ok"] (below) carries the
        # "everything healthy?" boolean for dashboards.
        return JobResult(
            job_name=ctx.job_name,
            run_id=ctx.run_id,
            ok=True,
            detail=(
                f"probed {len(results)} role(s); {len(failures)} drift"
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


def _load_bundle_safely() -> ConfigBundle:
    return load_config()


def _collect_active_roles(
    bundle: ConfigBundle,
) -> list[tuple[str, str, str]]:
    """Return ``(role_name, ref, kind)`` for every active role with a
    primary resolved.  Skips inactive, unresolved, or disabled-provider
    refs so the probe roster matches what's actually reachable."""
    out: list[tuple[str, str, str]] = []
    for role_name, role in bundle.roles.items():
        if role.mode != "active" or role.primary is None:
            continue
        conn = role.primary.connection
        if not conn.tier_enabled or not conn.enabled:
            continue
        out.append((role_name, role.primary.ref, role.primary.model.kind))
    # Also probe the embeddings ref — it's wired outside the ``roles``
    # block (see roles.yaml ``embeddings:`` top-level).
    emb = bundle.embeddings
    if emb is not None:
        conn = emb.connection
        if conn.tier_enabled and conn.enabled:
            out.append(("embeddings", emb.ref, emb.model.kind))
    return out


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
                "evidence": result.evidence,
                "source": result.source,
                "probed_at": result.probed_at,
            },
        )

    return _default_publisher


__all__ = ["ProviderProbeJob"]


# ── Direct-invoke entry point ───────────────────────────────────────
# The phase doc's §10 done-criterion: ``python -m
# tesseract.scheduler.tasks.provider_probe`` writes a row to each
# role's JSONL. The harness below mints a minimal JobContext and runs
# the orchestrator once.


def _main() -> int:
    import uuid

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
