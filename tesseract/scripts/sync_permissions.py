"""Reconcile `tesseract/config/permissions.yaml::tools:` with the live tool
registry's class-declared `default_posture` values.

Two modes:

    python -m tesseract.scripts.sync_permissions --check
        Exit 0 when registry and yaml agree (no orphans, every tool either
        has a yaml entry or a class default that matches). Exit 1 otherwise,
        printing a drift report. Suitable for CI.

    python -m tesseract.scripts.sync_permissions --write
        Round-trip-edit `permissions.yaml` to add an entry for every
        registered tool that's currently absent (using its class default).
        Operator overrides that diverge from the class default are
        preserved untouched. Orphan entries are listed but NEVER removed
        automatically — that's the operator's call.

Round-tripping uses `tesseract.lib.yaml_io.round_trip_yaml`, so comments
and key order are preserved. The yaml after `--write` always materializes
every registered tool, making the file the visible audit surface — but
the *runtime* truth still flows from the tool class, so this is a sync
operation, not a coupling.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from tesseract.brain.tools import ToolRegistry
from tesseract.kernel.tools.base import Tool
from tesseract.lib.yaml_io import round_trip_yaml
from tesseract.paths import CONFIG_DIR
from tesseract.permissions.policy import load_permission_policy

PERMISSIONS_YAML = CONFIG_DIR / "permissions.yaml"


def _build_registry_snapshot() -> dict[str, str]:
    """Harvest every concrete Tool subclass's class-declared
    ``default_posture`` together with the string its ``name`` property
    returns, without instantiating the tool.

    Many tools require runtime services (registries, adapters, vault
    managers) in ``__init__``, so we cannot just call ``cls().name``.
    Instead we parse the module's AST and look for a ``def name(self)``
    body that is a single ``return "<literal>"``. That covers every
    tool currently in the tree. Anything more dynamic would surface as
    an empty match and be reported as a sync miss.
    """
    import ast
    import importlib
    import pkgutil

    import tesseract.kernel.tools as pkg

    snapshot: dict[str, str] = {}
    for info in pkgutil.iter_modules(pkg.__path__, pkg.__name__ + "."):
        try:
            mod = importlib.import_module(info.name)
        except Exception as exc:
            logging.warning("sync_permissions: could not import %s: %s", info.name, exc)
            continue
        try:
            source = Path(mod.__file__).read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError, AttributeError):
            continue

        # Map ClassName → tool_name by walking AST `def name` bodies.
        ast_names: dict[str, str] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if not isinstance(item, ast.FunctionDef) or item.name != "name":
                    continue
                # Body shape: [Return(Constant(value="x"))]
                if len(item.body) == 1 and isinstance(item.body[0], ast.Return):
                    val = item.body[0].value
                    if isinstance(val, ast.Constant) and isinstance(val.value, str):
                        ast_names[node.name] = val.value

        for attr in dir(mod):
            cls = getattr(mod, attr)
            if not isinstance(cls, type) or not issubclass(cls, Tool) or cls is Tool:
                continue
            if cls.__abstractmethods__:
                continue
            # Skip imports — only count tools actually defined in this module.
            # Without this, a class imported by N modules generates N spurious
            # warnings (the AST table is per-module).
            if getattr(cls, "__module__", "") != mod.__name__:
                continue
            posture = getattr(cls, "default_posture", "")
            if posture not in ("auto", "ask", "deny"):
                raise SystemExit(
                    f"tool class {cls.__name__} has invalid default_posture={posture!r}"
                )
            tool_name = ast_names.get(cls.__name__)
            if not tool_name:
                # Factory-built tools (e.g. `channel_send._make_media_tool`)
                # have `def name(self): return name_` — a closure ref, not a
                # literal — so the AST scan misses them. Fall back to a
                # no-arg instantiation. Tools that need runtime services in
                # __init__ still surface as a miss and warn below.
                try:
                    tool_name = cls().name  # type: ignore[call-arg]
                except Exception:
                    tool_name = None
            if not tool_name:
                logging.warning(
                    "sync_permissions: could not determine tool name for %s "
                    "(no `def name(self): return \"...\"` and no-arg "
                    "instantiation failed)", cls.__name__,
                )
                continue
            snapshot[tool_name] = posture
    return snapshot


def _diff(class_defaults: dict[str, str], yaml_defaults: dict[str, str]) -> dict:
    registered = set(class_defaults)
    yaml_listed = set(yaml_defaults)
    return {
        "missing_in_yaml": sorted(registered - yaml_listed),
        "orphan_in_yaml": sorted(yaml_listed - registered),
        "diverged": sorted(
            name for name in (registered & yaml_listed)
            if class_defaults[name] != yaml_defaults[name]
        ),
        "registered": class_defaults,
        "yaml": yaml_defaults,
    }


def _print_report(report: dict) -> None:
    if report["missing_in_yaml"]:
        print("missing in yaml (will use class default at runtime):")
        for name in report["missing_in_yaml"]:
            print(f"  + {name} -> {report['registered'][name]}")
    if report["orphan_in_yaml"]:
        print("orphan in yaml (no such tool registered):")
        for name in report["orphan_in_yaml"]:
            print(f"  - {name} (yaml={report['yaml'][name]})")
    if report["diverged"]:
        print("operator override diverges from class default:")
        for name in report["diverged"]:
            print(
                f"  ~ {name}: yaml={report['yaml'][name]}, "
                f"class={report['registered'][name]}"
            )
    if not (report["missing_in_yaml"] or report["orphan_in_yaml"] or report["diverged"]):
        print("permissions.yaml is in sync with the tool registry.")


def _write_missing(missing: dict[str, str], path: Path) -> None:
    """Append every `name: posture` from `missing` to `tools:` via round-trip."""
    if not missing:
        return

    def mutate(doc):  # noqa: ANN001
        tools = doc.setdefault("tools", {})
        for name in sorted(missing):
            if name not in tools:
                tools[name] = missing[name]

    round_trip_yaml(path, mutate)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--check", action="store_true", help="report drift, exit 1 on any")
    grp.add_argument("--write", action="store_true", help="add missing tools to yaml")
    ap.add_argument(
        "--path",
        type=Path,
        default=PERMISSIONS_YAML,
        help="permissions.yaml path (default: %(default)s)",
    )
    args = ap.parse_args(argv)

    class_defaults = _build_registry_snapshot()
    policy = load_permission_policy(args.path)
    yaml_defaults = dict(policy.tools_defaults)
    report = _diff(class_defaults, yaml_defaults)
    _print_report(report)

    if args.check:
        drift = bool(report["missing_in_yaml"] or report["orphan_in_yaml"])
        return 1 if drift else 0

    if args.write:
        missing = {n: class_defaults[n] for n in report["missing_in_yaml"]}
        if missing:
            _write_missing(missing, args.path)
            print(f"\nwrote {len(missing)} entries to {args.path}")
        if report["orphan_in_yaml"]:
            print(
                "\nNOTE: orphan entries left untouched — remove them by hand "
                "if the tools are gone for good."
            )
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
