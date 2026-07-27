"""Janitor — fingerprint-and-orphan cleanup of processes, scratch dirs,
stale controller sessions, and aged archives (Docs/Plan/janitor/PLAN.md).

Three entry points share `runner.run_sweep`: the supervisor boot sweep,
the `janitor_sweep` scheduled job, and `python -m tesseract.scripts.cleanup`.
"""

from .config import JanitorConfig, load_janitor_config
from .models import Finding, SweepReport
from .runner import run_sweep

__all__ = [
    "Finding",
    "JanitorConfig",
    "SweepReport",
    "load_janitor_config",
    "run_sweep",
]
