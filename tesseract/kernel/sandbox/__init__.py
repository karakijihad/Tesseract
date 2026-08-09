"""Sandbox primitives.

``appcontainer`` / ``ipc_bridge`` (MO-8 provisional-tool sandbox) were
deleted with the forge/upgrades self-modification stack (prune wave 1,
Batch 2). ``_ipc_frames`` — the length-prefixed framing codec shared
with ``orchestrator.agent_controller`` — is unaffected and imported
directly from its submodule by callers.
"""
