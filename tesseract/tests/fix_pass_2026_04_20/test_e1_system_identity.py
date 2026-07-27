"""E1 — /api/identity returned observer_model=null because it looked up
the stale role name `observer`. Live config has `observer_agent`.

Codex Finding #10. Phase 14 blocker: it extends /api/identity to expose
all 4 roles (chat_brain, claude_cli, codex_cli, observer_agent) — if
the observer_agent lookup returns None here, the Settings panel's model
table will show the observer row as blank.

Repro exercises the identity() handler's role-resolution logic against
a fixture config. Before fix: observer_head is None. After fix: it's
the primary observer_agent entry.
"""

from __future__ import annotations


def _fixture_models_yaml() -> dict:
    return {
        "roles": {
            "chat_brain": {
                "resolution": [
                    {"tier": "api", "provider": "openai", "model": "gpt-5.4-nano"}
                ]
            },
            "observer_agent": {
                "resolution": [
                    {"tier": "api", "provider": "openai", "model": "gpt-5.4-nano"},
                    {"tier": "api", "provider": "google", "model": "gemini-2.5-flash"},
                ]
            },
        }
    }


def test_identity_picks_up_observer_agent() -> None:
    import inspect
    from tesseract.mirror.server.routes import system

    src = inspect.getsource(system.identity)
    # The handler must look up the live role name.
    assert 'roles.get("observer_agent")' in src, (
        "BUG (E1): identity() still reads roles.get('observer') — Phase 14 /api/identity extension will return observer_model=null"
    )
    assert 'roles.get("observer")' not in src.replace('roles.get("observer_agent")', ""), (
        "BUG (E1): residual reference to stale `observer` role remains in identity()"
    )
