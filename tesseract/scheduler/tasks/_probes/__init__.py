"""Probe substrate.

A probe is a known-good single-shot check of a ref that some active role
in ``roles.yaml`` names, whose ``ProbeResult`` says either "the model came
back with what you'd expect" or "something drifted." The orchestrator
(:mod:`tesseract.scheduler.tasks.provider_probe`) dispatches one probe
per distinct ref per tick and writes a row to the per-ref JSONL log at
``runtime/logs/provider-health/<ref>.jsonl``.

Drift-event drafting and apply are **not** here. The ``provider_watch``
mapper consumes the JSONL and the ``hot_config`` class handles
``providers.yaml`` patches drafted from this telemetry. This package
ships the signal only.
"""

from tesseract.scheduler.tasks._probes.base import ProbeResult, RoleProbe

__all__ = ["ProbeResult", "RoleProbe"]
