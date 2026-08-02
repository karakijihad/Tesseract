"""Per-iteration spend ceilings for scheduled loops (``permissions.yaml``).

`loop_cost_caps` is the authority. It used to be mirrored into a job's own
`schedule.yaml` block, which meant editing the policy file changed nothing —
the job read its own copy. One source, read at call time.
"""

from __future__ import annotations

import yaml


def load_loop_cost_caps() -> dict[str, object]:
    """The `loop_cost_caps` block from ``permissions.yaml``.

    Resolved via `config_dir()` at call time so a `TESSERACT_HOME` change is
    honoured without a fresh import. Raises loudly on a missing file or block:
    a silently defaulted spend ceiling is a budget leak, not a convenience.
    """
    from tesseract.paths import config_dir

    path = config_dir() / "permissions.yaml"
    if not path.exists():
        raise FileNotFoundError(f"permissions config not found: {path}")
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    caps = loaded.get("loop_cost_caps")
    if not isinstance(caps, dict):
        raise KeyError(f"'loop_cost_caps' block missing from {path}")
    return caps


def require_cap(caps: dict[str, object], key: str) -> float:
    """One cap by name, or a loud failure naming the key that is missing."""
    if key not in caps:
        raise KeyError(f"'{key}' missing from permissions.yaml::loop_cost_caps")
    try:
        return float(caps[key])  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be numeric, got {caps[key]!r}") from exc
