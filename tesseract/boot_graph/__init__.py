"""The boot graph: what the runtime prepares, in what order, and what blocks the window.

Two halves, deliberately split. `substrate.py` holds the facts a substrate's
own code declares — whether it holds the GIL, what happens when it is cold,
whether this machine has any use for it. `graph.py` holds the shape the
operator can re-order: which layers exist, what fires together, and where the
window opens.

Code declares the facts; YAML declares the shape. The same split
`Tool.default_posture` already holds against `permissions.yaml`.
"""

from tesseract.boot_graph.graph import (
    BootGraphError,
    BootReport,
    Layer,
    layers_for_reload,
    load_graph,
    run_layers,
    validate,
)
from tesseract.boot_graph.substrate import Substrate, SubstrateRegistry

__all__ = [
    "BootGraphError",
    "BootReport",
    "Layer",
    "Substrate",
    "SubstrateRegistry",
    "layers_for_reload",
    "load_graph",
    "run_layers",
    "validate",
]
