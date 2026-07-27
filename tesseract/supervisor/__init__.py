"""Out-of-process supervisor for the Mirror backend.

AU-1: the supervisor owns the backend lifecycle, distinguishes
operator-initiated shutdown from crashes, and refuses to respawn after
operator intent. Lives outside the backend so a backend crash can't
take the supervisor with it.

The supervisor MAY only: spawn the backend, signal it, kill it, read
``<TESSERACT_HOME>/runtime/intent.json``, write ``crash_storm.json``,
write its own log. No memory / vault / agenda / mission access — see
``Docs/Plan/autonomy/_shared/kill-switch-protocol.md`` for the full
contract.
"""
