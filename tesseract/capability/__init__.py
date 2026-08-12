"""What this machine has, what this version needs, and the gap between them.

One artifact, written by one pass, read by the surfaces that report it. The
package exists because the answer used to be computed eight separate times —
by the three voice fetchers, the reranker fetcher, `ensure_ollama`,
`provision_hardware`, `check_dependencies` and the updater — with no shared
result and nowhere to report to.

Nothing here downloads, installs or repairs. It decides what is true; acting
on that is the caller's, and whether a caller may act without asking is
`Consent`'s answer, not this package's.
"""

from tesseract.capability.consent import (
    ConsentAnswer,
    ConsentLedger,
    ledger_path,
    read_ledger,
    record,
)
from tesseract.capability.state import (
    AUTHORITATIVE_ORIGINS,
    CapabilityState,
    Consent,
    ConsentOrigin,
    DependencyRecord,
    DependencyState,
    HardwareFacts,
    VerifiedPin,
    read_state,
    state_path,
    write_state,
)

__all__ = [
    "AUTHORITATIVE_ORIGINS",
    "CapabilityState",
    "Consent",
    "ConsentAnswer",
    "ConsentLedger",
    "ConsentOrigin",
    "DependencyRecord",
    "DependencyState",
    "HardwareFacts",
    "VerifiedPin",
    "ledger_path",
    "read_ledger",
    "read_state",
    "record",
    "state_path",
    "write_state",
]
