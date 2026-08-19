"""Generate the Kernel panel's manifest from the live runtime.

Sibling of `generate_guide.py`, and written for the same reason.
The Kernel rail used to be a hand-traced node list, which is precisely how
its predecessor drifted: the panel kept naming a tool group that no longer
existed and kept claiming a tool count that had not been true for months.

What the runtime knows, the runtime supplies:

* **tool groups** — every name in the live registry, matched against the
  taxonomy in `cockpit.yaml::kernel.tool_groups`. A tool that matches no
  group, or more than one, is an error rather than a silent omission, so a
  newly registered tool cannot go missing from the panel.
* **the tool count** — what is actually registered, not a number in a comment.
* **delegate seats** — `roles.yaml::roles.coder` / `roles.auditor`. A seat is
  named by its provider and never by its model, so swapping the model behind a
  role does not rewrite the panel.
* **voice lanes** — `roles.yaml::voice.stt` / `voice.tts`.

The output is a BUILD-TIME asset: the frontend imports the JSON statically, so
the rail shows what the config said when the manifest was generated, not what
it says now. CI fails on drift, which keeps a checkout honest, but an operator
who edits `roles.yaml` in an installed app sees the old seat until the next
release. Serving the manifest from the runtime instead would close that;
until it does, nothing here may claim the rail follows live config.

Stages, not tools. A flow's stages are structural — they are the shape of
`chat.py`, not a runtime value — so the skeleton is declared here alongside
the `source` that was traced to write it. Everything a running system can
answer for itself is filled in from the system.

Usage:
    python -m tesseract.scripts.generate_kernel_manifest
    python -m tesseract.scripts.generate_kernel_manifest --check
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml

from tesseract.brain.boot import build_tool_registry
from tesseract.paths import ROOT

OUTPUT_PATH = (
    ROOT / "tesseract" / "mirror" / "src" / "cockpit" / "kernel" / "kernel-manifest.json"
)
COCKPIT_CONFIG = ROOT / "tesseract" / "config" / "cockpit.yaml"
ROLES_CONFIG = ROOT / "tesseract" / "config" / "roles.yaml"


class ManifestError(RuntimeError):
    """The runtime and the taxonomy disagree — never a silent fallback."""


def _load_yaml(path):
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _tool_group_spec() -> list[dict[str, Any]]:
    cockpit = _load_yaml(COCKPIT_CONFIG)
    try:
        groups = cockpit["kernel"]["tool_groups"]
    except KeyError as exc:
        raise ManifestError(
            f"{COCKPIT_CONFIG} is missing kernel.tool_groups — the Kernel rail "
            "cannot group the registry without it"
        ) from exc
    if not groups:
        raise ManifestError(f"{COCKPIT_CONFIG}: kernel.tool_groups is empty")
    return groups


def _ensure_env_independent_tools(registry) -> None:
    """`invoke_agent` + `session_open` only register when a chat adapter
    resolves, so a no-credential CI run counts two fewer tools than a dev
    checkout and `--check` fails on the difference rather than on drift.
    Register stubs when absent — only their names reach the manifest, and a
    name is a constant. `guide_facts` holds the same helper for the same
    reason; the copy stays because that module does not ship and this one
    does, so importing it would put a script in a user's tree that cannot
    import.
    """
    from tesseract.kernel.adapters.base import AdapterOptions
    from tesseract.kernel.tools.invoke_agent import InvokeAgentTool
    from tesseract.kernel.tools.session_tools import SessionOpenTool

    if registry.get("invoke_agent") is None:
        registry.register(InvokeAgentTool(
            agents_dir=ROOT,
            adapter=None,
            options=AdapterOptions(),
            parent_registry=registry,
            max_tool_iterations=1,
            max_consecutive_adapter_errors=1,
        ))
    if registry.get("session_open") is None:
        registry.register(SessionOpenTool())


@contextmanager
def _scratch_home():
    """Boot the registry against a throwaway state root.

    `build_tool_registry()` is not a read: it constructs `FTSIndex`, which
    creates directories and opens a SQLite database with WAL enabled. A
    command documented as "does not write" was therefore materialising
    memory-store state on every CI run, and failing outright on a read-only
    checkout.

    `CONFIG_DIR` follows `TESSERACT_HOME`, so the scratch root gets a copy of
    the real config tree — the registry reads the operator's actual roles and
    providers, and writes its indexes somewhere that is deleted on the way
    out. The tool NAME set does not depend on state, which is the only thing
    this script takes from the registry.
    """
    real_config = ROOT / "tesseract" / "config"
    previous = os.environ.get("TESSERACT_HOME")
    with tempfile.TemporaryDirectory(prefix="tesseract-manifest-") as tmp:
        shutil.copytree(real_config, Path(tmp) / "config")
        os.environ["TESSERACT_HOME"] = tmp
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("TESSERACT_HOME", None)
            else:
                os.environ["TESSERACT_HOME"] = previous


def _registered_tool_names() -> list[str]:
    with _scratch_home():
        registry, _mood, _bundle, _alarms = build_tool_registry()
    _ensure_env_independent_tools(registry)
    return sorted(schema["name"] for schema in registry.schemas_for_adapter())


def assign_groups(
    names: list[str], groups: list[dict[str, Any]]
) -> dict[str, list[str]]:
    """Match every registered tool to exactly one group.

    Ambiguity and omission are both errors. A tool matching two groups means
    the taxonomy is wrong; a tool matching none means the panel would draw a
    dispatch stage that cannot account for what fired under it.
    """
    assigned: dict[str, list[str]] = {group["id"]: [] for group in groups}
    unmatched: list[str] = []
    ambiguous: dict[str, list[str]] = {}

    for name in names:
        hits = [
            group["id"]
            for group in groups
            if any(fnmatch(name, pattern) for pattern in group["match"])
        ]
        if not hits:
            unmatched.append(name)
        elif len(hits) > 1:
            ambiguous[name] = hits
        else:
            assigned[hits[0]].append(name)

    if unmatched or ambiguous:
        problems = []
        if unmatched:
            problems.append(
                "tools matching no group: " + ", ".join(unmatched)
            )
        if ambiguous:
            problems.append(
                "tools matching several: "
                + "; ".join(f"{n} -> {gs}" for n, gs in ambiguous.items())
            )
        raise ManifestError(
            f"{COCKPIT_CONFIG}: kernel.tool_groups does not partition the live "
            "registry — " + " | ".join(problems)
        )

    return assigned


def _provider_of(ref: str) -> str:
    """`cli.claude.opus_5` -> `cli.claude`. The seat is named by its provider,
    never by the model: swapping the model must not rewrite the panel."""
    parts = ref.split(".")
    if len(parts) < 2:
        raise ManifestError(f"roles.yaml: malformed model ref {ref!r}")
    return ".".join(parts[:2])


def _seat_sub(roles: dict[str, Any], chains: dict[str, Any]) -> str:
    """The provider filling each delegation seat.

    Resolved through `loader.chain_refs`, not by reading `primary` off the
    block: a role names either its own primary or a shared `chain`, and this
    read `primary` alone until every seat moved onto a chain and it started
    raising on a config that boots perfectly well.
    """
    from tesseract.config.loader import ConfigError, chain_refs

    seats = []
    for name in ("coder", "auditor"):
        role = roles.get(name)
        if not role:
            raise ManifestError(f"roles.yaml: roles.{name} is missing")
        try:
            primary, _fallbacks = chain_refs(name, role, chains)
        except ConfigError as exc:
            raise ManifestError(f"roles.yaml: roles.{name} — {exc}") from exc
        seats.append(_provider_of(primary))
    return " · ".join(seats)


def _lane_sub(voice: dict[str, Any], lane: str) -> str:
    entry = voice.get(lane)
    if not entry or not entry.get("primary"):
        raise ManifestError(f"roles.yaml: voice.{lane}.primary is missing")
    return _provider_of(entry["primary"])


def build_manifest() -> dict[str, Any]:
    groups = _tool_group_spec()
    names = _registered_tool_names()
    assigned = assign_groups(names, groups)

    roles_cfg = _load_yaml(ROLES_CONFIG)
    roles = roles_cfg.get("roles") or {}
    chains = roles_cfg.get("chains") or {}
    voice = roles_cfg.get("voice") or {}

    group_nodes = []
    for group in groups:
        node: dict[str, Any] = {
            "id": f"g-{group['id']}",
            "label": group["label"],
            "kind": group["kind"],
            "depth": 2,
            "tools": assigned[group["id"]],
        }
        if group.get("tone"):
            node["tone"] = group["tone"]
        group_nodes.append(node)

    turn_nodes: list[dict[str, Any]] = [
        {"id": "input", "label": "operator input", "sub": "typed or spoken",
         "kind": "stage", "tone": "accent", "signal": "turn.start"},
        {"id": "stt", "label": "STT", "sub": _lane_sub(voice, "stt"),
         "kind": "stage", "depth": 1, "signal": "voice.stt"},
        {"id": "preflight", "label": "cost preflight", "sub": "blocks or asks",
         "kind": "gate", "tone": "warn", "signal": "cost.preflight"},
        {"id": "drains", "label": "drains", "sub": "comments · spawns",
         "kind": "stage", "signal": "turn.drains"},
        {"id": "recall", "label": "auto-recall", "sub": "top-k memories",
         "kind": "stage", "signal": "memory.recall"},
        {"id": "prompt", "label": "prompt assembled", "kind": "stage",
         "signal": "turn.prompt"},
        {"id": "stream", "label": "stream", "sub": "text · tool calls",
         "kind": "stage", "tone": "accent", "signal": "turn.stream"},
        {"id": "permission", "label": "permission", "sub": "AUTO · ASK · DENY",
         "kind": "gate", "depth": 1, "tone": "warn", "signal": "tool.permission"},
        {"id": "dispatch", "label": "tool dispatch",
         "sub": f"{len(names)} registered", "kind": "stage", "depth": 1,
         "signal": "tool.dispatch"},
        *group_nodes,
        {"id": "result", "label": "result appended",
         "sub": "↺ back to stream, to the cap", "kind": "stage", "depth": 1,
         "signal": "tool.result"},
        {"id": "tts", "label": "TTS lane", "sub": _lane_sub(voice, "tts"),
         "kind": "stage", "depth": 1, "signal": "voice.tts"},
        {"id": "persist", "label": "persist history", "kind": "stage",
         "signal": "turn.persist"},
        {"id": "observer", "label": "observer", "sub": "suggestions → next turn",
         "kind": "store", "tone": "info", "signal": "observer.fire"},
    ]

    return {
        "generator": "tesseract/scripts/generate_kernel_manifest.py",
        "flows": [
            {
                "id": "turn",
                "label": "Turn",
                "source": "brain/chat.py::send · brain/tools.py::execute_tool "
                          "· lib/voice/{stt-stream,tts-player}.ts",
                "nodes": turn_nodes,
            },
            {
                "id": "memory",
                "label": "Memory",
                "source": "memory/retrieval.py::RetrievalPipeline · memory/reranker.py",
                "nodes": [
                    {"id": "query", "label": "query", "kind": "stage",
                     "signal": "memory.query"},
                    {"id": "exact", "label": "exact slug",
                     "sub": "stage 0 · skips the rest", "kind": "stage",
                     "tone": "ok", "signal": "memory.exact"},
                    {"id": "prefilter", "label": "prefilter", "sub": "stage A",
                     "kind": "stage", "signal": "memory.prefilter"},
                    {"id": "bm25", "label": "BM25 index",
                     "sub": "stage B · full index", "kind": "store", "depth": 1,
                     "tone": "info", "signal": "memory.bm25"},
                    {"id": "vector", "label": "vector index",
                     "sub": "stage B · full index", "kind": "store", "depth": 1,
                     "tone": "info", "signal": "memory.vector"},
                    {"id": "rerank", "label": "rerank",
                     "sub": "stage C · when installed", "kind": "stage",
                     "signal": "memory.rerank"},
                    {"id": "block", "label": "[recalled_memories]",
                     "sub": "one turn only", "kind": "stage", "tone": "ok",
                     "signal": "memory.injected"},
                ],
            },
            {
                "id": "delegation",
                "label": "Delegates",
                "source": "roles.yaml::roles.coder / roles.auditor "
                          "· kernel/tools/delegate_*.py",
                "nodes": [
                    {"id": "brain", "label": "chat brain", "sub": "tool call",
                     "kind": "stage", "signal": "tool.dispatch"},
                    {"id": "gate", "label": "permission", "kind": "gate",
                     "tone": "warn", "signal": "tool.permission"},
                    {"id": "seat", "label": "the seat", "sub": _seat_sub(roles, chains),
                     "kind": "seat", "tone": "accent", "signal": "delegate.spawn"},
                    {"id": "lane", "label": "lane surface",
                     "sub": "streams while it runs", "kind": "stage", "depth": 1,
                     "signal": "delegate.stream"},
                    {"id": "completion", "label": "completion",
                     "sub": "injected next turn, not awaited", "kind": "stage",
                     "tone": "accent", "signal": "delegate.complete"},
                ],
            },
        ],
    }


def render() -> str:
    return json.dumps(build_manifest(), indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if the manifest is stale (for CI). Does not write.",
    )
    args = parser.parse_args()

    try:
        rendered = render()
    except ManifestError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    if args.check:
        if not OUTPUT_PATH.exists():
            print(f"[stale] {OUTPUT_PATH} does not exist", file=sys.stderr)
            return 1
        if OUTPUT_PATH.read_text(encoding="utf-8").strip() != rendered.strip():
            print(
                f"[stale] {OUTPUT_PATH} drifted from the runtime; "
                "run `python -m tesseract.scripts.generate_kernel_manifest`",
                file=sys.stderr,
            )
            return 1
        print(f"[ok] {OUTPUT_PATH} matches the live runtime")
        return 0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(rendered, encoding="utf-8")
    print(f"[wrote] {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
