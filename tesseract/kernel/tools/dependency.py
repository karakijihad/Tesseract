"""What a tool cannot work without, resolved to the name its breaker takes.

A tool's breaker IS its dependency's breaker, and a dependency's name is the
one the catalog already gives it. That single decision is what keeps coverage
off a list: a provider added to `providers.yaml` names its own breaker, and a
tool written afterwards inherits whichever ref its declared role resolves to.
Nothing registers anything, and `breaker_status` / `breaker_reset` reach the
result because they already reach every name in `context/circuit_breaker.py`.

Three shapes, and the empty one is a declaration too:

  ``role:<key>``     a `roles.yaml` role, resolved to its primary's catalog ref
                     (`cli.codex.gpt56_terra`). Moving the role to another
                     provider moves the breaker with it, with no edit here.
  ``service:<key>``  a `providers.yaml::services` entry, resolved to the
                     ACCOUNT prefix its operations are logged under,
                     ``api.<key>``. Tavily writes health to `api.tavily.search`
                     and `api.tavily.extract`; both narrow to `api.tavily`
                     through `account_of`, which is the same name this
                     resolves to. A separate `service.<key>` namespace would
                     have been a second name for one account, which is the
                     defect this file exists to prevent.
  ``""``             nothing is behind this tool. Its failures are its
                     caller's, it is never gated and never counted.

**An unknown key raises; an inactive role does not.** A typo is the config
being wrong and the operator has to see it, which is the house rule about
missing keys. A role deliberately switched off is not an error, and a boot that
died because somebody deactivated a role would be this module causing the
outage it exists to bound. An inactive role resolves to `None`, so the tool is
ungated and fails its own way, which is where a wiring problem belongs.
"""

from __future__ import annotations

from typing import Any

ROLE_PREFIX = "role:"
SERVICE_PREFIX = "service:"

#: The tier a paid HTTP service's operations are logged under. Its health rows
#: are `api.<service>.<operation>` (`api.tavily.search`), so the account they
#: belong to is `api.<service>` — the same string `account_of` produces from
#: one of those rows. There is no separate service namespace, deliberately.
SERVICE_TIER = "api"


class DependencyError(ValueError):
    """A `depends_on` the runtime cannot resolve. Raised at boot."""


#: `(providers stamp, roles stamp) -> the bundle built from them`. One entry.
_BUNDLE_CACHE: tuple[tuple[Any, Any], Any] | None = None


def _stamp(path: Any) -> Any:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _bundle() -> Any:
    """The config bundle, rebuilt only when a config file has changed.

    `load_config` caches the YAML PARSE but rebuilds the whole bundle on every
    call: it re-resolves every ref and rebuilds every role. Measured 3.14 ms
    here, and this is asked twice per gated tool call, once per chain entry at
    construction, and once per tool at boot. A stat is microseconds and the
    config watcher's write changes the mtime, so freshness survives.
    """
    global _BUNDLE_CACHE
    from tesseract.config.loader import load_config
    from tesseract.paths import config_dir

    root = config_dir()
    # The DIRECTORY is part of the key, not only the stamps. `TESSERACT_HOME`
    # moves between tests and `seed_real_config_home` copies one source file
    # into many temp homes, so two different homes routinely present the same
    # mtime and size. Keyed on stamps alone this cache would hand a bundle
    # built against one home to a caller reading another.
    key = (str(root), _stamp(root / "providers.yaml"), _stamp(root / "roles.yaml"))
    # A file we could not stat is a question this cache must not answer.
    if None in key:
        return load_config()
    if _BUNDLE_CACHE is not None and _BUNDLE_CACHE[0] == key:
        return _BUNDLE_CACHE[1]
    bundle = load_config()
    _BUNDLE_CACHE = (key, bundle)
    return bundle


def reset_bundle_cache() -> None:
    """Drop the held bundle. For tests that move `TESSERACT_HOME` between
    cases, where two homes can present the same mtime pair."""
    global _BUNDLE_CACHE
    _BUNDLE_CACHE = None


def resolve(depends_on: str) -> str | None:
    """The breaker name behind `depends_on`, or None when nothing is.

    Raises `DependencyError` on an unknown shape, an unknown role or an
    unknown service, so a mistyped declaration fails at boot beside the
    posture and group checks rather than at the first call that needed it.
    """
    declared = (depends_on or "").strip()
    if not declared:
        return None

    if declared.startswith(ROLE_PREFIX):
        name = declared[len(ROLE_PREFIX):].strip()
        if not name:
            raise DependencyError(f"{declared!r} names no role")
        bundle = _bundle()
        if name not in bundle.roles:
            raise DependencyError(
                f"depends_on={declared!r} names a role that is not in "
                f"roles.yaml. Roles are the wiring; add it there or fix the "
                f"spelling."
            )
        role = bundle.roles[name]
        if role.mode != "active" or role.primary is None:
            return None
        return role.primary.ref

    if declared.startswith(SERVICE_PREFIX):
        name = declared[len(SERVICE_PREFIX):].strip()
        if not name:
            raise DependencyError(f"{declared!r} names no service")
        services = _bundle().providers_raw.get("services") or {}
        if name not in services or not isinstance(services.get(name), dict):
            raise DependencyError(
                f"depends_on={declared!r} names a service that is not in "
                f"providers.yaml::services."
            )
        return f"{SERVICE_TIER}.{name}"

    raise DependencyError(
        f"depends_on={declared!r} is not a shape this runtime knows. Use "
        f"{ROLE_PREFIX}<roles.yaml key>, {SERVICE_PREFIX}<providers.yaml "
        f"services key>, or \"\" for a tool with nothing behind it."
    )


def catalog_ref(tier: str, provider: str, model_id: str) -> str:
    """`<tier>.<provider>.<catalog key>` for a model known by its own id.

    The catalog keys an entry by a name of its own (`gpt56_terra`) and the
    entry carries the id the provider answers to (`gpt-5.6-terra`). Callers
    that hold only the id were building the ref by joining what they had, so
    a lane failure wrote health to `cli.codex.gpt-5.6-terra` while the probe
    wrote to `cli.codex.gpt56_terra`: two files for one seat, and the one
    anybody reads stayed green. Measured 2026-09-02.

    Falls back to the id when no entry claims it, because a row under a
    slightly wrong name still beats no row.
    """
    try:
        tier_block = (_bundle().providers_raw.get(tier) or {}).get(provider) or {}
        for key, entry in (tier_block.get("models") or {}).items():
            if isinstance(entry, dict) and entry.get("model") == model_id:
                return f"{tier}.{provider}.{key}"
    except Exception:  # noqa: BLE001 — a name is not worth failing a caller
        pass
    return f"{tier}.{provider}.{model_id}"


def account_of(name: str) -> str:
    """The name a quota or a login belongs to, given a dependency name.

    A subscription is spent per ACCOUNT, not per model: `cli.codex` is out of
    quota, and `cli.codex.gpt56_terra` is one way of asking it. Recording a
    `usage` or `auth` fault against the model would shut one entry and leave a
    second role on the same exhausted account spending against nothing.

    Every other kind stays on the full name, because `not_found` and `schema`
    really are about the one entry. The caller picks by kind; this only knows
    how to shorten.
    """
    parts = name.split(".")
    if len(parts) >= 3:
        return ".".join(parts[:2])
    return name


__all__ = [
    "DependencyError",
    "ROLE_PREFIX",
    "SERVICE_TIER",
    "SERVICE_PREFIX",
    "account_of",
    "resolve",
]
