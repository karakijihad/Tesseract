"""The retention table — what ages, for how long, and what happens then.

`policy.py` is the catalog and the loader; `sweeps.py` is one function per
tree. The `retention` stage of the nightly `consolidate` row runs them.
"""

from tesseract.retention.policy import (
    Action,
    Policy,
    RetentionError,
    Swept,
    TREES,
    Tree,
    load_live,
    load_policies,
)

__all__ = [
    "Action",
    "Policy",
    "RetentionError",
    "Swept",
    "TREES",
    "Tree",
    "load_live",
    "load_policies",
]
