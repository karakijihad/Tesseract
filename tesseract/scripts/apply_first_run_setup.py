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
  Identity tab's picker writes;
- the API keys go into `<TESSERACT_HOME>/.env`, seeded from the shipped
  template first so the operator keeps the documented file rather than one
  holding only what the form collected.

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


def apply_keys(answers: Mapping[str, Any]) -> list[str]:
    """Write the form's API keys into `<TESSERACT_HOME>/.env`.

    Seeds the file first: `ensure_env_seeded` copies the shipped template and
    is a no-op once that copy exists, so doing it here — rather than leaving
    it to the backend start that happens later — means the keys land in the
    documented file instead of creating a bare one the seeder would then
    decline to replace.

    Blank values are skipped rather than written as empty. That is what makes
    this safe to re-run: a re-provision replays a staged file whose secrets
    have since been redacted, and writing those blanks back would clear keys
    the operator has set in Settings in the meantime.

    Names are checked against the template, so a hand-edited answers file
    cannot use this path to set an arbitrary environment variable for the
    next boot.
    """
    from tesseract import env_file
    from tesseract.config_seed import ensure_env_seeded

    raw = answers.get("api_keys")
    if not isinstance(raw, Mapping):
        return []
    wanted = {
        str(name): str(value).strip()
        for name, value in raw.items()
        if str(value or "").strip()
    }
    if not wanted:
        return []

    ensure_env_seeded()

    known = {spec.name for spec in env_file.parse_example()}
    unknown = sorted(set(wanted) - known)
    if unknown:
        logger.warning("%s: ignoring keys not in .env.example: %s", _LABEL, ", ".join(unknown))
    wanted = {name: value for name, value in wanted.items() if name in known}

    if not wanted:
        return []

    try:
        return env_file.set_values(wanted)
    except OSError as exc:
        # Never fatal: the operator can set every one of these in Settings ->
        # API keys, which is a worse first run than the one they asked for but
        # a working one.
        logger.warning("%s: could not write .env (%s) — keys not applied", _LABEL, exc)
        return []


def _kokoro_model_ids() -> list[str]:
    from tesseract.config.loader import load_config

    block = (load_config().providers_raw.get("local") or {}).get("kokoro") or {}
    return sorted(block.get("models") or {})


def _redact(path: Path, answers: Mapping[str, Any]) -> bool:
    """Strip the secrets out of the staged file, in place, before it is kept.

    `_consume` archives rather than deletes, so without this the keys the
    operator typed on the setup form would live on in `runtime/` forever, in
    plaintext, beside the `.env` that is supposed to be the one place they
    are. The key NAMES stay — they are the record of what the form asked —
    and only the values go.

    Returns whether the file is safe to keep. False means it still holds
    secrets, and the caller must destroy it rather than archive it.
    """
    if not isinstance(answers.get("api_keys"), Mapping) or not answers["api_keys"]:
        return True
    redacted = {**answers, "api_keys": {name: "" for name in answers["api_keys"]}}
    body = json.dumps(redacted, indent=2)
    try:
        from tesseract.lib.yaml_io import atomic_write_text

        atomic_write_text(path, body, prefix=".first-run-")
        return True
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("%s: atomic redaction of %s failed (%s) — overwriting in place", _LABEL, path, exc)

    # The atomic write replaces a sibling tempfile, which Windows refuses
    # while any handle on the target is open. Truncating the file we already
    # have needs no rename, so it survives the case that just failed — and
    # truncation happens first, which is what makes a half-written fallback
    # safe: the worst outcome is an empty file, never a file still holding
    # the tail of the keys.
    try:
        with path.open("w", encoding="utf-8") as handle:
            handle.write(body)
        return True
    except OSError as exc:
        logger.error(
            "%s: could not redact %s (%s) — removing it instead of keeping it",
            _LABEL,
            path,
            exc,
        )
        return False


def _consume(path: Path, *, archive: bool = True) -> None:
    """Retire the answers file so it is applied exactly once.

    `provision()` runs again whenever an install fails its health check — a
    deleted venv, a quarantined interpreter — and re-applying the form would
    then silently revert every choice the operator has changed since, in the
    Identity tab or by hand. Renamed rather than deleted so the record of
    what the install was set up with survives on the machine.

    `archive=False` when the file could not be redacted: the record is a
    convenience and the secrets in it are not, so an unredactable file is
    destroyed rather than kept.

    Deleting is the fallback because retiring this file is what enforces
    once-only, and keeping the record is only a convenience. Windows refuses
    a rename while any handle on the file is open, and losing that race would
    leave the answers live to be reapplied over a rename the operator has
    since made — the exact outcome this function exists to prevent.
    """
    if archive:
        try:
            path.replace(path.with_name(f"{path.stem}.applied{path.suffix}"))
            return
        except OSError as exc:
            logger.warning(
                "%s: could not archive %s (%s) — removing it instead", _LABEL, path, exc
            )
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        if archive:
            logger.error(
                "%s: could not retire %s (%s). It will be applied again if this "
                "install ever re-provisions, which would revert any identity or "
                "voice change made since. Delete it by hand.",
                _LABEL,
                path,
                exc,
            )
            return
        # Redaction failed and now removal has too, by two different
        # mechanisms — a rewrite and an unlink. Nothing else in this process
        # is going to succeed where both failed, so the honest end of the
        # line is to say exactly what is on disk rather than to keep trying
        # and report success.
        logger.critical(
            "%s: %s STILL HOLDS the API keys typed on the setup form — it "
            "could be neither redacted nor removed (%s). Delete it by hand.",
            _LABEL,
            path,
            exc,
        )


#: Form answer -> the dependency the reconciler knows it by. The voice and
#: listening answers are here too, because they are download decisions even
#: though they are asked as a choice between engines rather than a checkbox.
_CONSENT_FROM_ANSWERS = {
    "embeddings": ("ollama", "ollama-models"),
    "reranker": ("reranker",),
    "gpu": ("gpu-acceleration",),
}

#: Form answer -> the `local.<provider>` switch that actually stops the
#: download. Recording consent is not enough on its own and that was a real
#: defect: the fetch scripts read the CATALOG, never the ledger —
#: `ensure_ollama` via `boot.ollama_refs()`, `fetch_reranker_model` via
#: `roles.yaml::reranker`, and `provision_hardware::wanted_extras` via
#: `local.<provider>.enabled`. So a decline that only reached the ledger left
#: every one of them downloading exactly as before.
#:
#: This is the same mechanism `apply_voice` already uses for tts/stt, extended
#: to the three answers step 2 added. The ledger still records WHY; this is
#: what makes the answer bite.
_SWITCHES_FROM_ANSWERS = {
    "embeddings": ("ollama",),
    "reranker": ("onnx_reranker",),
    # No provider of its own: `wanted_extras` drops an extra whose consumers
    # are all disabled, and both consumers are the speech engines the operator
    # answered separately. Declining acceleration therefore cannot be
    # expressed as a switch without also turning off the engine it
    # accelerates, so it is recorded as consent only and `provision_hardware`
    # is left to decide from the hardware.
    "gpu": (),
}


def apply_optional_switches(answers: Mapping[str, Any]) -> list[str]:
    """Turn the step-2 declines into the config switches the fetchers read.

    Returns what changed. A missing `optional` block changes nothing, which is
    what an install predating step 2 must do.
    """
    optional = answers.get("optional")
    if not isinstance(optional, Mapping):
        return []

    wanted: dict[str, bool] = {}
    for key, providers in _SWITCHES_FROM_ANSWERS.items():
        if key not in optional:
            continue
        for provider in providers:
            wanted[provider] = bool(optional[key])
    if not wanted:
        return []

    from tesseract.lib.yaml_io import round_trip_yaml
    from tesseract.paths import config_dir

    path = config_dir() / "providers.yaml"
    if not path.exists():
        logger.warning("%s: %s missing - optional switches not applied", _LABEL, path)
        return []

    changed: list[str] = []

    def _apply(doc: Any) -> None:
        local = doc.get("local")
        if not isinstance(local, dict):
            raise KeyError("local")
        for provider, enabled in wanted.items():
            block = local.get(provider)
            if not isinstance(block, dict):
                logger.warning(
                    "%s: providers.yaml has no local.%s - skipping", _LABEL, provider
                )
                continue
            if bool(block.get("enabled", True)) == enabled:
                continue
            block["enabled"] = enabled
            changed.append(f"local.{provider}.enabled={str(enabled).lower()}")

    try:
        round_trip_yaml(path, _apply)
    except KeyError as exc:
        logger.warning("%s: providers.yaml has no %s - switches not applied", _LABEL, exc)
        return []
    return changed


def apply_consent(answers: Mapping[str, Any]) -> list[str]:
    """Record what the operator agreed to download, as an ANSWER.

    The config writes above already decide what gets fetched. This records
    *why*, and that is not the same fact: `enabled: false` cannot tell a lane
    someone declined from one nobody ever reached, and the reconciler needs
    the difference to know what it may repair without asking. Without this,
    every dependency on a fresh install would read as `never_asked` forever
    and the launch pass would go on treating the config as the only signal.

    Best-effort. A ledger that cannot be written leaves the install working —
    consent then falls back to what config implies, which is exactly today's
    behaviour — so this must never fail a first run.
    """
    from tesseract.capability.consent import record
    from tesseract.capability.state import Consent, ConsentOrigin

    decisions: dict[str, Consent] = {}

    engine = str(answers.get("tts") or "").strip().lower()
    if engine:
        for name in ("kokoro", "piper"):
            decisions[name] = (
                Consent.GRANTED if engine == name else Consent.DECLINED
            )
    if "stt" in answers:
        decisions["whisper"] = (
            Consent.GRANTED if bool(answers.get("stt")) else Consent.DECLINED
        )

    optional = answers.get("optional")
    if isinstance(optional, Mapping):
        for key, dependencies in _CONSENT_FROM_ANSWERS.items():
            if key not in optional:
                continue
            answer = Consent.GRANTED if bool(optional[key]) else Consent.DECLINED
            for dependency in dependencies:
                decisions[dependency] = answer

    if not decisions:
        return []
    try:
        record(decisions, origin=ConsentOrigin.FIRST_RUN)
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s: could not record consent (%s)", _LABEL, exc)
        return []
    granted = sorted(k for k, v in decisions.items() if v is Consent.GRANTED)
    declined = sorted(k for k, v in decisions.items() if v is Consent.DECLINED)
    return [f"consent granted={','.join(granted) or '-'} declined={','.join(declined) or '-'}"]


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

    # Redaction runs whatever happens above it. `apply_identity` and
    # `apply_voice` both raise on a config missing the block they write, and
    # `main` catches everything and exits 0 — so without the `finally` a
    # failed identity write would leave the operator's API keys staged in
    # plaintext, permanently, in the file this function exists to retire.
    try:
        applied = (
            apply_identity(answers)
            + apply_voice(answers)
            + apply_optional_switches(answers)
            + apply_keys(answers)
            + apply_consent(answers)
        )
        logger.info("%s: applied %s", _LABEL, ", ".join(applied) or "nothing")
    finally:
        _consume(target, archive=_redact(target, answers))
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
