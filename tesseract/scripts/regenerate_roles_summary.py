"""Regenerate ``vault/knowledge-base/roles/SUMMARY.md`` from the catalog.

Pure-Python — no LLM, no Tavily. Reads ``roles.yaml`` + ``providers.yaml``,
renders a role → primary → fallbacks → adapter table. Atomic write.

Called from:
- Mirror app boot (``mirror/server/app.py::_on_startup``) after config load.
- The ``yaml_change_proposal`` apply path on any successful catalog edit
  (MO-10-2).

CLI entry: ``python -m tesseract.scripts.regenerate_roles_summary``.
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from tesseract.knowledge_keeper.scaffolding import ensure_kb_tree, kb_root
from tesseract.lib.yaml_io import atomic_write_text
from tesseract.paths import CONFIG_DIR

log = logging.getLogger(__name__)


def _config_path(name: str) -> Path:
    return CONFIG_DIR / name


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        return {}
    return data


def _resolve_adapter(catalog: dict[str, Any], ref: str) -> str:
    """Look up the adapter string for a ``<tier>.<provider>.<model>`` ref.

    Returns ``"?"`` when any segment is missing — keeps the renderer
    forgiving against catalog drift; the actual fail-loud path lives in
    ``boot.build_adapter``.
    """
    if not ref or not isinstance(ref, str):
        return "?"
    parts = ref.split(".")
    if len(parts) < 3:
        return "?"
    tier, provider, _model = parts[0], parts[1], ".".join(parts[2:])
    tier_block = catalog.get(tier)
    if not isinstance(tier_block, dict):
        return "?"
    prov = tier_block.get(provider)
    if not isinstance(prov, dict):
        return "?"
    adapter = prov.get("adapter")
    return str(adapter) if adapter else "?"


def _render_section_heading(level: int, text: str) -> str:
    return f"{'#' * level} {text}"


def render_summary(roles_doc: dict[str, Any], providers_doc: dict[str, Any]) -> str:
    """Build the SUMMARY.md text. Pure function — easy to test."""
    now = datetime.now(timezone.utc).isoformat()
    buf = io.StringIO()
    buf.write("---\n")
    buf.write(f"generated_at: {now}\n")
    buf.write("source: tesseract/config/roles.yaml + providers.yaml\n")
    buf.write("---\n\n")
    buf.write("# the assistant roles — current wiring\n\n")
    buf.write(
        "Auto-regenerated on Mirror boot and on every approved "
        "`yaml_change_proposal`. Hand-edits will be overwritten.\n\n"
    )

    embeddings = roles_doc.get("embeddings") or {}
    if isinstance(embeddings, dict) and embeddings.get("primary"):
        buf.write("## Embeddings\n\n")
        buf.write(f"- primary: `{embeddings.get('primary')}`\n\n")

    buf.write("## Cognition roles\n\n")
    buf.write("| Role | Mode | Primary | Fallbacks | Adapter |\n")
    buf.write("|------|------|---------|-----------|---------|\n")
    roles = roles_doc.get("roles") or {}
    if isinstance(roles, dict):
        for name, body in roles.items():
            if not isinstance(body, dict):
                continue
            mode = str(body.get("mode") or "?")
            primary = str(body.get("primary") or "")
            fallbacks = body.get("fallbacks") or []
            if not isinstance(fallbacks, list):
                fallbacks = []
            adapter = _resolve_adapter(providers_doc, primary) if primary else "?"
            primary_cell = f"`{primary}`" if primary else "—"
            fb_cell = ", ".join(f"`{f}`" for f in fallbacks) if fallbacks else "—"
            buf.write(f"| {name} | {mode} | {primary_cell} | {fb_cell} | {adapter} |\n")
    buf.write("\n")

    voice = roles_doc.get("voice") or {}
    if isinstance(voice, dict):
        buf.write("## Voice lanes\n\n")
        buf.write("| Lane | Mode | Primary | Fallbacks |\n")
        buf.write("|------|------|---------|-----------|\n")
        for lane in ("stt", "tts"):
            block = voice.get(lane)
            if not isinstance(block, dict):
                continue
            mode = str(block.get("mode") or "?")
            primary = str(block.get("primary") or "")
            fallbacks = block.get("fallbacks") or []
            if not isinstance(fallbacks, list):
                fallbacks = []
            primary_cell = f"`{primary}`" if primary else "—"
            fb_cell = ", ".join(f"`{f}`" for f in fallbacks) if fallbacks else "—"
            buf.write(f"| {lane} | {mode} | {primary_cell} | {fb_cell} |\n")
        buf.write("\n")

    return buf.getvalue()


def regenerate(
    *,
    roles_path: Path | None = None,
    providers_path: Path | None = None,
    target: Path | None = None,
) -> Path:
    """Read the catalog, render SUMMARY.md, write atomically. Returns the
    target path.
    """
    rp = roles_path if roles_path is not None else _config_path("roles.yaml")
    pp = providers_path if providers_path is not None else _config_path("providers.yaml")
    out = target if target is not None else (ensure_kb_tree() / "roles" / "SUMMARY.md")
    out.parent.mkdir(parents=True, exist_ok=True)

    roles_doc = _load_yaml(rp)
    providers_doc = _load_yaml(pp)
    text = render_summary(roles_doc, providers_doc)
    atomic_write_text(out, text)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Regenerate roles/SUMMARY.md")
    parser.add_argument("--roles", type=Path, default=None)
    parser.add_argument("--providers", type=Path, default=None)
    parser.add_argument("--target", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        out = regenerate(
            roles_path=args.roles,
            providers_path=args.providers,
            target=args.target,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("regenerate_roles_summary failed: %s", exc)
        return 1
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
