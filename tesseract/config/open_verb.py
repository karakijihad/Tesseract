"""`open` verb config (``open_verb.yaml``).

Config-as-authority: every key is required — the loader
raises on a missing or malformed value rather than substituting a hardcoded
infrastructure default. Mirrors ``config/mcp.py`` and ``config/cockpit.py``.

The search endpoint and the app allowlist live here specifically so neither is
a source literal: swapping search engines or granting a new launchable app is a
YAML edit, not a code change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from tesseract.paths import config_dir

# Refused whatever the config says. ShellExecute on any of these is arbitrary
# code execution, and `.lnk`/`.url`/`.library-ms` are indirection formats whose
# target is not the file being validated — the check would inspect the shortcut
# and the OS would run something else entirely.
FORBIDDEN_LAUNCH_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".exe", ".com", ".bat", ".cmd", ".ps1", ".psm1", ".psd1", ".vbs",
        ".vbe", ".js", ".jse", ".wsf", ".wsh", ".ws", ".wsc", ".msi",
        ".msp", ".msc", ".scr", ".cpl", ".hta", ".jar", ".reg", ".pif",
        ".gadget", ".inf", ".sct", ".chm",
        # Interpreter targets. On a machine with the runtime installed these
        # are double-click-to-execute, and every TESSERACT box has Python.
        ".py", ".pyw", ".pyz", ".pl", ".rb", ".lua", ".ahk",
        # ClickOnce launchers.
        ".application", ".appref-ms",
        # Indirection: what gets validated is not what the OS then runs.
        ".lnk", ".url", ".library-ms", ".search-ms", ".settingcontent-ms",
    }
)


class OpenConfig(BaseModel):
    """Validated ``open_verb.yaml``. Extra keys are rejected so a typo is a
    boot error rather than a setting that silently does nothing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    search_url: str
    probe_timeout_s: float = Field(gt=0)
    apps: dict[str, str]
    blocked_networks: frozenset[str]
    launch_extensions: frozenset[str]

    @field_validator("search_url")
    @classmethod
    def _must_carry_query_placeholder(cls, v: str) -> str:
        if "{query}" not in v:
            raise ValueError("search_url must contain the '{query}' placeholder")
        return v

    @field_validator("blocked_networks")
    @classmethod
    def _parseable_networks(cls, v: frozenset[str]) -> frozenset[str]:
        """A malformed CIDR would silently stop blocking anything, which is the
        worst failure mode for a denylist."""
        import ipaddress

        for entry in v:
            ipaddress.ip_network(entry, strict=False)
        return v

    @field_validator("apps")
    @classmethod
    def _apps_are_absolute(cls, v: dict[str, str]) -> dict[str, str]:
        """A bare name is resolved by ShellExecute against the app-paths
        registry, PATH and the working directory, so a dropped executable can
        win the lookup. Requiring an absolute path removes the search."""
        relative = sorted(k for k, path in v.items() if not Path(path).is_absolute())
        if relative:
            raise ValueError(
                f"apps entries must map to an absolute path (a bare name is "
                f"resolved against PATH and is hijackable): {relative}"
            )
        return v

    @field_validator("launch_extensions")
    @classmethod
    def _no_executables(cls, v: frozenset[str]) -> frozenset[str]:
        """A config edit must not be able to make ShellExecute run code. The
        launch path enforces this again at call time; failing here as well
        means the operator learns at boot, not at the moment it matters."""
        lowered = frozenset(e.lower() for e in v)
        forbidden = lowered & FORBIDDEN_LAUNCH_EXTENSIONS
        if forbidden:
            raise ValueError(
                f"launch_extensions may not include executable, script or "
                f"indirection types: {sorted(forbidden)}"
            )
        if any(not e.startswith(".") for e in lowered):
            raise ValueError("launch_extensions entries must start with '.'")
        return lowered


def _config_path() -> Path:
    """Call-time so a `TESSERACT_HOME` change after import is honored."""
    return config_dir() / "open_verb.yaml"


def load_open_config() -> OpenConfig:
    path = _config_path()
    if not path.is_file():
        raise FileNotFoundError(f"open verb config missing: {path}")
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a mapping")
    missing = {
        "search_url",
        "probe_timeout_s",
        "apps",
        "launch_extensions",
        "blocked_networks",
    } - raw.keys()
    if missing:
        raise KeyError(f"{path} missing required key(s): {sorted(missing)}")
    return OpenConfig(**raw)
