"""Build-time templater: emit the shipped default-config tree.

Every `tesseract/config/<name>.yaml` that ships is in exactly one of two
states, and a file in neither is a hard build FAILURE:

- **Templated** — a hand-authored `_shipping/<name>.yaml` ships verbatim,
  because the dev file holds something a stranger must not receive. That is
  usually a per-install value rather than a secret: `security_mode: headless`
  is right for this machine and would be a serious regression shipped to
  someone who has not yet seen the assistant work.
- **Folded** — named in `FOLDED` below, and the dev file ships as it stands.
  Most config never differed between the two trees except in comments written
  for a maintainer of this repo rather than a user of the app; keeping a second
  copy of those files bought nothing and drifted, so production shipped stale
  settings while the template looked maintained.

The list is explicit on purpose. The rule this module exists to enforce is that
the operator's live config never reaches production by *accident* — a silent
fallback for any file missing a template is precisely the leak path the
templating replaced. Folding is a deliberate, reviewed declaration that one
named file is the same for everyone; it is not a default.

`audit_release_tree.scan_all` still runs over the built tree and hard-fails on
PII, secrets or work notes, so a folded file that later grows something private
fails the build rather than shipping.

Never touches the source directory; this is a one-way, read-src/write-out step.
"""

from __future__ import annotations

import shutil
from pathlib import Path

# Config that is identical for this machine and every install, so the dev file
# IS the shipped file. Add a name here only after checking the two are the same
# but for comments — `diff <(grep -v '^\\s*#' a) <(grep -v '^\\s*#' b)` empty.
FOLDED = frozenset({
    "agenda-mappers.yaml",
    "hardware.yaml",
    "janitor.yaml",
    "mcp_servers.yaml",
    "memory.yaml",
    "open_verb.yaml",
    "providers.yaml",
    "roles.yaml",
    "runtime.yaml",
    "terminal.yaml",
    "tokenjuice.yaml",
})


def build_shipping_config(src_dir: Path, out_dir: Path) -> None:
    """Write the shipped config tree into `out_dir`.

    A templated file ships from `src_dir/_shipping/<name>.yaml`; a `FOLDED`
    one ships from `src_dir/<name>.yaml`. Anything else is a hard build
    failure — never a silent fallback to the operator's live file.
    """
    if src_dir.resolve() == out_dir.resolve():
        raise ValueError(f"build_shipping_config: src_dir and out_dir must differ (both resolve to {src_dir.resolve()})")
    out_dir.mkdir(parents=True, exist_ok=True)
    shipping_dir = src_dir / "_shipping"
    for src_file in src_dir.glob("*.yaml"):
        name = src_file.name
        template = shipping_dir / name
        if template.is_file():
            shutil.copy2(template, out_dir / name)
        elif name in FOLDED:
            shutil.copy2(src_file, out_dir / name)
        else:
            raise RuntimeError(
                f"build_shipping_config: {name} has no shipping template "
                f"(expected {template}) and is not in FOLDED — refusing to "
                "fall back to the operator's live config. Either write the "
                "template or fold the file deliberately."
            )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("src_dir", type=Path)
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args()
    build_shipping_config(args.src_dir, args.out_dir)
