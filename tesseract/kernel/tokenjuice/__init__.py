"""TokenJuice — tool-output compression layer (AU-15).

Single chokepoint between Tool.run() and the adapter input. Classifies the
tool result by (tool_name, tool_args), applies a chain of pure reducers,
and audits the before/after token counts. Port of vincentkoc/tokenjuice
rule schema with TARS-specific reducer kinds.

Wired once in tesseract/brain/tools.py::execute_tool. The adapter side
(tesseract/kernel/adapters/base.py) is the abstract; it never sees raw
Tool.run output, so the actual chokepoint is brain/tools.py.
"""

from __future__ import annotations

from .audit import audit_dir, count_tokens, write_audit
from .config import (
    BUILTIN_RULES_DIR,
    TOKENJUICE_YAML,
    TokenJuiceConfig,
    load_config,
    project_rules_dir,
    user_rules_dir,
)
from .engine import ProcessResult, process
from .reducers import REDUCERS, TRANSFORMS
from .rules import Rule, classify, load_rules

__all__ = [
    "BUILTIN_RULES_DIR",
    "TOKENJUICE_YAML",
    "ProcessResult",
    "REDUCERS",
    "Rule",
    "TRANSFORMS",
    "TokenJuiceConfig",
    "audit_dir",
    "classify",
    "count_tokens",
    "load_config",
    "load_rules",
    "process",
    "project_rules_dir",
    "user_rules_dir",
    "write_audit",
]
