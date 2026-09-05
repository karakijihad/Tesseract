"""What the Kernel rail draws, built from the config as it stands.

**This used to be a file on disk and that was the defect.** A script wrote
`kernel-manifest.json` into the source tree and the frontend imported it, so
the rail showed the seats the app was BUILT with. CI checked the committed
copy against a dev checkout, which kept this repo honest and did nothing at all
for an operator who edits `roles.yaml` in an installed app: they changed a seat
and the panel went on drawing the old one, with no error to notice.

So the runtime answers instead. `GET /api/cockpit/kernel-manifest` calls this
against the live tool registry and the config tree the operator owns, and the
panel asks again when the config watcher says a file changed. There is one
source and it is the same one the rest of the runtime reads.

What it takes from where:

* **tool groups** --- the taxonomy in `cockpit.yaml::kernel.tool_groups`. Every
  registered tool must match exactly one; matching none or several raises,
  because a dispatch stage that cannot account for what fired under it is worse
  than an error somebody has to fix.
* **the tool count** --- what is registered, not a number in a comment.
* **delegate seats** --- `roles.yaml::roles.coder` / `roles.auditor`, named by
  PROVIDER and never by model, so swapping the model behind a role does not
  rewrite the panel.
* **voice lanes** --- `roles.yaml::voice.stt` / `voice.tts`.

Stages are structural: a flow's steps are the shape of `chat.py`, not a runtime
value, so the skeleton is declared here alongside the `source` that was traced
to write it. Everything a running system can answer for itself is filled in
from the system.
"""

from __future__ import annotations

from collections.abc import Sequence
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml

from tesseract.paths import config_dir, system_config_dir


class ManifestError(RuntimeError):
    """The runtime and the taxonomy disagree. Never a silent fallback."""


def _cockpit_config() -> Path:
    """The taxonomy is the APP's: which groups the rail draws is not a per
    machine choice, and `cockpit.yaml` is not seeded into the operator's tree.
    """
    return system_config_dir() / "cockpit.yaml"


def _roles_config() -> Path:
    """The seats are THEIRS. In an install this is the file first-run setup
    wrote and the operator has owned since; in this checkout the two are the
    same path, which is exactly why the old build-time read looked correct.

    A fresh install has no home file yet and reads the shipped one, which is
    right. So does an install whose home file was DELETED, which is not, and
    the two are indistinguishable from here: the rail would quietly revert to
    the app's seats and read as though the operator's edits had been undone.
    Nothing in the payload says which was read. Left as it is because the
    alternative is refusing to draw the rail on a fresh install, and written
    down because a reader will otherwise assume the fallback is only ever the
    first case.
    """
    theirs = config_dir() / "roles.yaml"
    return theirs if theirs.is_file() else system_config_dir() / "roles.yaml"


def config_paths() -> tuple[Path, ...]:
    """Every file this module can name in an error.

    The route strips these from the text it returns, and it strips them by
    asking rather than by recognising a path when it sees one. Two files, both
    resolved the same way the builder resolves them, so the pair cannot drift.
    """
    return (_cockpit_config(), _roles_config())


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def registry_tools(registry: Any) -> tuple[list[str], list[str]]:
    """What the app shipped, and what this machine added, as two name lists.

    **They are separated because only one of them can be a taxonomy fault.**
    `cockpit.yaml` is the app's file, in the sealed tree, and an install cannot
    edit it. So a SHIPPED tool it cannot file is a defect somebody has to fix
    before release, and raising is right. A tool the operator wrote is not: it
    was never going to be in a file they cannot open, and refusing to draw the
    panel until it is would take the whole rail off an install for the crime
    of authoring a tool, which is a thing this app invites them to do.

    The old build-time generator never met the question because it built its
    registry with `include_home_tools` defaulted off, so a custom tool reached
    the rail neither as a group nor as a break. The runtime reads the live
    registry, where they are present and where `origin` already tells them
    apart (`kernel/home_tools.py::_is_custom` uses the same field).

    Read off the registry's own dict rather than through
    `schemas_for_adapter`, which serialises every tool's full JSON schema to
    hand back a name the dict is already keyed by. Free at build time, paid
    per request here.
    """
    shipped: list[str] = []
    custom: list[str] = []
    for name, tool in registry.tools.items():
        target = custom if getattr(tool, "origin", "shipped") == "custom" else shipped
        target.append(str(name))
    return shipped, custom


def _tool_group_spec() -> list[dict[str, Any]]:
    cockpit = _load_yaml(_cockpit_config())
    try:
        groups = cockpit["kernel"]["tool_groups"]
    except KeyError as exc:
        raise ManifestError(
            f"{_cockpit_config()} is missing kernel.tool_groups - the Kernel "
            "rail cannot group the registry without it"
        ) from exc
    if not groups:
        raise ManifestError(f"{_cockpit_config()}: kernel.tool_groups is empty")
    return groups


def assign_groups(
    names: Sequence[str], groups: list[dict[str, Any]]
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
            f"{_cockpit_config()}: kernel.tool_groups does not partition the live "
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


def build_manifest(
    names: Sequence[str], custom: Sequence[str] = ()
) -> dict[str, Any]:
    """The Kernel rail's picture, off the config as it is on disk right now.

    `names` is what the app shipped and is partitioned strictly. `custom` is
    what this machine added; it is drawn in its own group and is never a
    reason to refuse the picture. `registry_tools` is what separates them.

    Both are parameters rather than reads because the callers get them
    differently and neither should build a registry the other's way: the route
    has one already, and a test passes literals.
    """
    groups = _tool_group_spec()
    names = sorted(names)
    assigned = assign_groups(names, groups)

    roles_cfg = _load_yaml(_roles_config())
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

    # The operator's own tools, drawn only when there are some. An empty group
    # on every stock install would be a box that says nothing; the group
    # appearing IS the fact that this machine has tools the app did not ship.
    if custom:
        group_nodes.append(
            {
                "id": "g-custom",
                "label": "yours",
                "kind": "stage",
                "depth": 2,
                "tone": "accent",
                "tools": sorted(str(name) for name in custom),
            }
        )

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
         "sub": f"{len(names) + len(custom)} registered", "kind": "stage",
         "depth": 1, "signal": "tool.dispatch"},
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
