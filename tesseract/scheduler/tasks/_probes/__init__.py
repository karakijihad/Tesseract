"""AU-14 probe substrate.

A probe is a known-good single-shot call against an active role from
``roles.yaml`` whose ``ProbeResult`` says either "the model came back
with what you'd expect" or "something drifted." The orchestrator
(:mod:`tesseract.scheduler.tasks.provider_probe`) dispatches one probe
per active role per tick and writes a row to the per-role JSONL log at
``runtime/logs/provider-health/<role>.jsonl``.

Drift-event drafting + apply is **not** here. AU-5's ``provider_watch``
mapper consumes the JSONL; AU-8's ``hot_config`` class handles
``providers.yaml`` patches drafted from this telemetry. AU-14 ships
the signal only.
"""

from tesseract.scheduler.tasks._probes.base import ProbeResult, RoleProbe

__all__ = ["ProbeResult", "RoleProbe"]
