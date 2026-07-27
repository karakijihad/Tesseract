"""Regression: the controller daemon must cold-boot without UnboundLocalError.

Live-boot bug surfaced 2026-05-25 (the audit M-4 "no live daemon run" gap): a
redundant function-local ``from ... import SessionRegistry`` inside
``run_controller``'s teardown ``finally`` made ``SessionRegistry`` a local for
the WHOLE function, so the earlier ``registry=SessionRegistry()`` at daemon
construction raised ``UnboundLocalError`` and the daemon crashed on every cold
boot:

    UnboundLocalError: cannot access local variable 'SessionRegistry'
    where it is not associated with a value

The name is imported once at module scope; nothing inside ``run_controller``
may rebind it (a function-local import does). This guards the whole class of
"local import shadows a module global used earlier in the function".
"""

from __future__ import annotations

from tesseract.scripts.tars_controller import run_controller


def test_run_controller_does_not_shadow_session_registry() -> None:
    # If SessionRegistry is bound anywhere inside run_controller (e.g. a
    # function-local import), Python treats every use as local → the earlier
    # construction at daemon-build time raises UnboundLocalError on cold boot.
    assert "SessionRegistry" not in run_controller.__code__.co_varnames, (
        "SessionRegistry is a function-local of run_controller — a local "
        "rebinding (likely a redundant `from ... import SessionRegistry`) will "
        "raise UnboundLocalError before daemon construction. It is already "
        "imported at module scope; use that."
    )
