"""Write the first-run setup form's answers into the operator's config.

The splash form runs in the Tauri shell, before there is any config to
write: `home/config/` is seeded from templates that live in the source
tree, and the source tree does not exist until the clone stage has run.
So the shell stages the answers as JSON in `runtime/` and this script —
invoked once, after the editable install makes `tesseract` importable —
seeds the config tree and then applies them.

Applied as *config*, never as a flag some other component checks later:

- the names and the gender go to `mirror.yaml::identity`, which is what
  renders everywhere and what the workspace templates are seeded with —
  the gender answer already picked the voice, and this is the same answer
  reaching what the agent is told about itself;
- declining an engine writes `enabled: false` on its `providers.yaml`
  provider, so the fetch scripts find nothing to download and the voice
  runtime skips the lane — and Settings → Capabilities can flip it back on
  later without this script or a reinstall;
- the voice choice writes `roles.yaml::voice.tts.primary`, the same key the
  Identity tab's picker writes.

Idempotent, and safe to re-run: applying the same answers twice produces
the same config. Never fails provisioning — a setup this could not apply
leaves the shipped defaults in place, which is a working install with a
name the operator did not choose rather than no install at all.

Usage: python -m tesseract.scripts.apply_first_run_setup [--answers PATH]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

_LABEL = "first-run setup"

# The engines the form offers, mapped to the `providers.yaml` provider that
# implements each. The form sends one of these keys, or "none".
_TTS_PROVIDERS = {"kokoro": "kokoro", "piper": "piper"}


def answers_path() -> Path:
    """Where the shell stages the form's answers.

    Under `runtime/` rather than `home/`: it describes what happened on
    *this* machine's install and must not travel with `home/` to another PC.
    """
    from tesseract.paths import runtime_dir

    return runtime_dir() / "first-run-setup.json"


def load_answers(path: Path) -> dict | None:
    """Read the staged answers, or None when there are none to apply.

    A missing file is the normal case on every launch after the first, and
    on any install whose shell predates the setup form. A malformed one is
    logged and treated the same way — the shipped defaults are a working
    config, and refusing to boot over a bad JSON file would be worse than
    an unnamed agent.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("%s: could not read %s (%s) — shipped defaults kept", _LABEL, path, exc)
        return None
    if not isinstance(raw, dict):
        logger.warning("%s: %s is not a JSON object — shipped defaults kept", _LABEL, path)
        return None
    return raw


def _clean_name(raw: Any) -> str:
    return str(raw or "").strip()


def apply_identity(answers: Mapping[str, Any]) -> list[str]:
    """Write the names and the wake prefix into `mirror.yaml::identity`.

    A blank answer leaves the shipped value alone rather than writing an
    empty name: `config_seed.identity_values()` raises on a blank name, and
    the workspace templates are rendered from these, so an empty one would
    seed a document with a missing word in every sentence.

    The prefix is the first half of the wake phrase, which is always two
    words. It is asked at setup so both halves are the operator's — a
    phrase reading `hey <their name>` is only half chosen.
    """
    from tesseract.lib.yaml_io import round_trip_yaml
    from tesseract.paths import config_dir

    from tesseract.config_seed import PRONOUNS

    wanted = {
        "name": _clean_name(answers.get("agent_name")),
        "operator_name": _clean_name(answers.get("operator_name")),
        # The same answer that picked the voice, now also reaching what the
        # agent is told about itself. An unrecognised value is dropped rather
        # than written, so the shipped neutral default stands instead of a
        # gender nothing downstream can map to pronouns.
        "gender": _clean_name(answers.get("gender")).lower(),
    }
    if wanted["gender"] not in PRONOUNS:
        wanted["gender"] = ""
    wanted = {key: value for key, value in wanted.items() if value}
    prefix = _clean_name(answers.get("wake_prefix"))
    if not wanted and not prefix:
        return []

    path = config_dir() / "mirror.yaml"
    if not path.exists():
        logger.warning("%s: %s missing — cannot apply names", _LABEL, path)
        return []

    applied_prefix = False

    def _apply(doc: Any) -> None:
        nonlocal applied_prefix
        identity = doc.get("identity")
        if identity is None:
            raise KeyError("identity")
        for key, value in wanted.items():
            identity[key] = value
        if not prefix:
            return
        # Written into the existing block rather than creating one: the
        # threshold beside it is a required key, so a half-block would
        # fail `load_identity` at the next read. A config somehow missing
        # the block keeps its names and loses only the prefix — refusing
        # the whole write would cost the operator their name over the
        # smaller of the two answers.
        block = identity.get("wake_word")
        if isinstance(block, dict):
            block["prefix"] = prefix
            applied_prefix = True

    round_trip_yaml(path, _apply)
    if prefix and not applied_prefix:
        logger.warning(
            "%s: %s has no identity.wake_word block — prefix not applied",
            _LABEL,
            path,
        )
    return sorted([*wanted, *(["wake_word.prefix"] if applied_prefix else [])])


def _tts_voice_ref(provider: str, gender: str) -> str | None:
    """The catalog ref for `provider`'s voice matching `gender`.

    Read from the catalog rather than hardcoded so adding a voice to
    `providers.yaml` makes it selectable here too. An unmatched gender
    returns None and the shipped default voice stands — a preference we
    cannot honour is not a reason to leave the operator mute.
    """
    from tesseract.config.loader import load_config

    bundle = load_config()
    block = (bundle.providers_raw.get("local") or {}).get(provider) or {}
    matches = [
        model_id
        for model_id, entry in (block.get("models") or {}).items()
        if str((entry or {}).get("kind")) == "tts"
        and str((entry or {}).get("gender", "")).lower() == gender
    ]
    if not matches:
        return None
    return f"local.{provider}.{sorted(matches)[0]}"


def apply_voice(answers: Mapping[str, Any]) -> list[str]:
    """Enable the chosen engines and select the voice that speaks.

    Choosing the lighter engine also drops the heavier one from the
    fallback chain: a lane left in the chain has its model downloaded, so
    keeping Kokoro as Piper's fallback would fetch the 338 MB the operator
    just declined. The reverse is not symmetric — Piper stays behind Kokoro,
    because it is small and it is what still speaks on a machine too slow
    to synthesize with Kokoro in real time.
    """
    from tesseract.lib.yaml_io import round_trip_yaml
    from tesseract.paths import config_dir
    from tesseract.voice.lane_config import apply_tts_primary, drop_tts_fallbacks

    engine = str(answers.get("tts") or "").strip().lower()
    gender = str(answers.get("gender") or "").strip().lower()
    want_stt = bool(answers.get("stt"))
    changed: list[str] = []

    enabled = {name: engine == name for name in _TTS_PROVIDERS}
    if engine == "kokoro":
        enabled["piper"] = True  # the lane that still speaks on a slow machine
    enabled["whisper"] = want_stt

    providers_path = config_dir() / "providers.yaml"
    if providers_path.exists():

        def _apply_switches(doc: Any) -> None:
            local = doc.get("local")
            if local is None:
                raise KeyError("local")
            for provider, on in enabled.items():
                block = local.get(provider)
                if block is None:
                    logger.warning("%s: providers.yaml has no local.%s", _LABEL, provider)
                    continue
                block["enabled"] = on

        round_trip_yaml(providers_path, _apply_switches)
        changed.extend(
            f"local.{provider}.enabled={str(on).lower()}"
            for provider, on in sorted(enabled.items())
        )

    roles_path = config_dir() / "roles.yaml"
    if engine in _TTS_PROVIDERS and roles_path.exists():
        ref = _tts_voice_ref(_TTS_PROVIDERS[engine], gender) if gender else None
        heavier: set[str] = set()
        if engine == "piper":
            heavier = {f"local.kokoro.{model_id}" for model_id in _kokoro_model_ids()}

        def _apply_lane(doc: Any) -> None:
            if ref:
                apply_tts_primary(doc, ref)
            if heavier:
                drop_tts_fallbacks(doc, heavier)

        if ref or heavier:
            round_trip_yaml(roles_path, _apply_lane)
            if ref:
                changed.append(f"voice.tts.primary={ref}")
            if heavier:
                changed.append("voice.tts.fallbacks-=kokoro")
    return changed


def _kokoro_model_ids() -> list[str]:
    from tesseract.config.loader import load_config

    block = (load_config().providers_raw.get("local") or {}).get("kokoro") or {}
    return sorted(block.get("models") or {})


def _consume(path: Path) -> None:
    """Retire the answers file so it is applied exactly once.

    `provision()` runs again whenever an install fails its health check — a
    deleted venv, a quarantined interpreter — and re-applying the form would
    then silently revert every choice the operator has changed since, in the
    Identity tab or by hand. Renamed rather than deleted so the record of
    what the install was set up with survives on the machine.

    Deleting is the fallback because retiring this file is what enforces
    once-only, and keeping the record is only a convenience. Windows refuses
    a rename while any handle on the file is open, and losing that race would
    leave the answers live to be reapplied over a rename the operator has
    since made — the exact outcome this function exists to prevent.
    """
    try:
        path.replace(path.with_name(f"{path.stem}.applied{path.suffix}"))
        return
    except OSError as exc:
        logger.warning("%s: could not archive %s (%s) — removing it instead", _LABEL, path, exc)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.error(
            "%s: could not retire %s (%s). It will be applied again if this "
            "install ever re-provisions, which would revert any identity or "
            "voice change made since. Delete it by hand.",
            _LABEL,
            path,
            exc,
        )


def apply_setup(path: Path | None = None) -> bool:
    """Seed the config tree, then apply the staged answers to it.

    Returns True when answers were found and applied. Seeding runs either
    way: this is the first thing after the editable install that can create
    `home/config/`, and the fetch stages behind it read from there.
    """
    from tesseract.config_seed import ensure_config_seeded

    ensure_config_seeded()

    target = path or answers_path()
    answers = load_answers(target)
    if answers is None:
        logger.info("%s: no staged answers — shipped defaults kept", _LABEL)
        return False

    applied = apply_identity(answers) + apply_voice(answers)
    logger.info("%s: applied %s", _LABEL, ", ".join(applied) or "nothing")
    _consume(target)
    return True


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--answers", help="path to the staged answers JSON")
    args = parser.parse_args()
    try:
        apply_setup(Path(args.answers) if args.answers else None)
    except Exception as exc:  # noqa: BLE001
        # Never fails provisioning: the shipped defaults are a working
        # install with a name the operator did not pick, which beats no
        # install at all. The Identity tab can set every one of these later.
        logger.warning("%s could not be applied (%s) — shipped defaults kept", _LABEL, exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
