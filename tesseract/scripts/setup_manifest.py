"""What the setup form should ask THIS machine, as JSON on stdout.

The form used to run before anything existed — before the clone, before
Python — so every figure on it was a literal in `splash.html` and a drift
test held each one honest against the catalog it could not read. It could
not know what the machine was either, which is why it quoted 1,600 MB of
speech recognition to a laptop about to download 148.

The order changed: the required half is installed as progress and the form
opens afterwards, on a tree that exists. So the page asks this module instead
of carrying copies. Sizes come from `providers.yaml`, the speech model comes
from resolving `hardware.yaml` against a real probe, and the key rows come
from the two files that declare which capability is gated on which key.

**Read-only.** It seeds `home/config/` — that has to happen before anything
can be read — and then reports. Nothing here writes an answer, installs a
package or fetches a byte; the operator has not been asked anything yet.

The prose lives here rather than in config or in the page, and that is
deliberate on both sides. In config it would be UI copy in a file whose job
is wiring. In the page it would be one more copy across a language boundary,
which is exactly what this module exists to end. Here it sits beside the
switch maps in `apply_first_run_setup` that every row must agree with, and a
test asserts the agreement.

Usage: python -m tesseract.scripts.setup_manifest
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)

_LABEL = "setup manifest"

#: The optional downloads the form offers as a plain yes/no, in the order they
#: are shown: `id -> what it buys`. Every id must be a key of
#: `apply_first_run_setup._SWITCHES_FROM_ANSWERS`, or the row is a control that
#: writes nothing — the dead-control defect this release already ruled against
#: once. `test_setup_manifest.py` is what enforces that.
#:
#: The NAME comes from `capability.models.DOWNLOAD_LABELS`, shared with
#: Settings → Capabilities: a capability that can be turned on in two places
#: reads as two different things if the two name it differently.
#:
#: Speech is not here: it is asked as a choice between where the voice runs
#: rather than as an on/off, and its rows are built by `_speech` below.
_EXTRA_ROWS: tuple[tuple[str, str], ...] = (
    (
        "embeddings",
        "Finds things by meaning rather than by wording, across its memory and "
        "your files. Without it, search falls back to matching words.",
    ),
    (
        "reranker",
        "Re-orders what search finds so the useful answer is first. Small, and "
        "it earns its size.",
    ),
    (
        "browser",
        "Lets it open a page and read, click and fill it in for you. Without "
        "it, it can still hand a link to your own browser — it just cannot "
        "read the page itself.",
    ),
)

#: `ENV_NAME -> (group, label, signup url, what the key buys)`.
#:
#: Which keys are ASKED FOR is not decided here — `_key_rows` derives that from
#: `providers.yaml` and `channels.yaml`, so a provider added to the catalog
#: reaches the form without a second edit. This map only supplies the words. A
#: key with no entry is still shown, named after its variable, because asking
#: for it unexplained beats not asking at all — and the suite says so.
_KEY_PROSE: dict[str, tuple[str, str, str, str]] = {
    "OPENAI_API_KEY": (
        "Talking to you",
        "OpenAI",
        "https://platform.openai.com/signup",
        "The one key the shipped setup needs to hold a conversation.",
    ),
    "BUILD_NVIDIA_KEY": (
        "Talking to you",
        "NVIDIA build",
        "https://build.nvidia.com/",
        "Free, and no payment method. Background work runs here — without it "
        "that work falls back to OpenAI and you pay for what you never see.",
    ),
    "GOOGLE_API_KEY": (
        "Talking to you",
        "Google AI Studio",
        "https://aistudio.google.com",
        "Carries more of the shipped setup than any other single key: several "
        "background roles, cloud speech recognition, the cloud voice, and "
        "image generation.",
    ),
    "ANTHROPIC_API_KEY": (
        "Talking to you",
        "Anthropic",
        "https://console.anthropic.com/settings/keys",
        "An alternative chat model. Nothing points at it until you say so.",
    ),
    "XAI_API_KEY": (
        "Talking to you",
        "xAI",
        "https://console.x.ai",
        "An alternative chat model, and the image-generation fallback.",
    ),
    # Two tools, two keys, and neither falls back to the other — `web_search`
    # is wired to Brave and `tavily_search`/`tavily_extract` to Tavily. Asked
    # here because the failure otherwise arrives as an error mid-answer, the
    # first time you ask it to look something up.
    "BRAVE_SEARCH_API_KEY": (
        "Reaching the web",
        "Brave Search",
        "https://brave.com/search/api/",
        "Free for 2,000 searches a month. Without it, searching the web fails "
        "with a setup hint instead of an answer.",
    ),
    "TAVILY_API_KEY": (
        "Reaching the web",
        "Tavily",
        "https://tavily.com",
        "Free for 1,000 a month. A separate tool: it pulls a page's full text "
        "in so it can be filed and searched later.",
    ),
    "TELEGRAM_BOT_TOKEN": (
        "Messaging",
        "Telegram bot",
        "https://t.me/BotFather",
        "Only if you want to reach it from your phone. Create a bot with "
        "@BotFather and paste the token. The allowlist it needs is set "
        "afterwards in Settings, since your chat id only exists once you have "
        "messaged the bot.",
    ),
}

#: The order the groups appear in. A group `_KEY_PROSE` does not place is
#: appended after these, so a new one is late rather than lost.
_KEY_GROUPS = ("Talking to you", "Reaching the web", "Messaging")


def gated_key_names() -> list[str]:
    """Every `.env` key a capability is gated on, from the files that say so.

    Three sources, because a key is declared beside the thing that needs it:
    model providers and outside services in `providers.yaml`, and each
    channel's own token in `channels.yaml`. Derived rather than listed, which
    is what makes a provider added to the catalog reach the form on its own.
    """
    import yaml

    from tesseract.paths import config_dir

    names: set[str] = set()
    providers = yaml.safe_load((config_dir() / "providers.yaml").read_text("utf-8")) or {}
    for section in ("api", "services"):
        for block in (providers.get(section) or {}).values():
            if isinstance(block, dict) and block.get("api_key_env"):
                names.add(str(block["api_key_env"]))

    channels = yaml.safe_load((config_dir() / "channels.yaml").read_text("utf-8")) or {}
    for name, block in channels.items():
        if name != "defaults" and isinstance(block, dict) and block.get("api_key_env"):
            names.add(str(block["api_key_env"]))
    return sorted(names)


def _key_rows() -> list[dict[str, str]]:
    ordered: list[dict[str, str]] = []
    remaining = set(gated_key_names())
    groups = [*_KEY_GROUPS]
    groups += sorted(
        {prose[0] for env, prose in _KEY_PROSE.items() if env in remaining}
        - set(_KEY_GROUPS)
    )
    for group in groups:
        for env, (row_group, label, url, hint) in _KEY_PROSE.items():
            if env not in remaining or row_group != group:
                continue
            remaining.discard(env)
            ordered.append(
                {"env": env, "group": group, "label": label, "url": url, "hint": hint}
            )
    # A key the catalog gates a capability on and this module has no words for.
    # Shown anyway: an unexplained field is a poor row, and a capability that
    # silently fails on first use is a worse install.
    for env in sorted(remaining):
        ordered.append(
            {
                "env": env,
                "group": "Other",
                "label": env,
                "url": "",
                "hint": "Needed by one of the capabilities this install ships.",
            }
        )
    return ordered


def _machine() -> dict[str, Any]:
    """This machine, and the profile `hardware.yaml` resolves it to.

    Best-effort in both halves. A probe that fails and a config that will not
    parse both land on the floor profile, which is what an unprofiled machine
    gets anyway — the form is then quoting the CPU payload, which is the
    smaller claim of the two and the safer one to be wrong about.
    """
    from tesseract.scripts.check_dependencies import _detect_gpu
    from tesseract.scripts.provision_hardware import load_hardware_config, select_profile

    try:
        gpu = _detect_gpu()
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s: could not probe the graphics card (%s)", _LABEL, exc)
        gpu = None

    cfg = load_hardware_config()
    profile = select_profile(cfg, gpu)
    return {
        "gpu_vendor": getattr(gpu, "vendor", "") or "",
        "gpu_name": getattr(gpu, "name", "") or "",
        "vram_mb": int(getattr(gpu, "memory_mb", 0) or 0),
        "profile": profile.name,
        "stt_model": profile.stt_model,
    }


def _speech(machine: dict[str, Any], sizes: dict[str, int]) -> dict[str, Any]:
    """The two speech questions, worded for the machine that is being asked.

    The local-voice hint is the one that changes: it used to promise every
    operator that speech "starts talking fastest on a machine with a graphics
    card", which is a fact about someone else's computer on a machine with no
    card. The probe has run by now, so it can say which of the two this is.
    """
    accelerated = machine["profile"] != "cpu"
    kokoro_mb = sizes.get("kokoro")
    whisper_mb = sizes.get("whisper")
    local_hint = "Speaks without an account and without a bill."
    if kokoro_mb:
        local_hint += f" Downloads {kokoro_mb} MB."
    local_hint += (
        " This machine's graphics card runs it, so it starts speaking quickly."
        if accelerated
        else " This machine has no supported graphics card, so it starts "
        "speaking a moment later than the cloud voice does."
    )
    return {
        "tts": {
            "default": "kokoro",
            "options": [
                {
                    "id": "kokoro",
                    "label": "On this machine",
                    "size_mb": kokoro_mb,
                    "hint": local_hint,
                },
                {
                    "id": "cloud",
                    "label": "In the cloud",
                    "size_mb": 0,
                    # Said plainly, and this is the one hint that has to be:
                    # choosing it with no Google key — here or later — gets
                    # replies as text, which reads as a broken install rather
                    # than a missing key.
                    "hint": "Downloads nothing and speaks well on any machine. "
                    "Needs a Google API key, and is billed per second of "
                    "speech — without a key it cannot speak at all.",
                },
                {
                    "id": "none",
                    "label": "Off",
                    "size_mb": 0,
                    "hint": "Replies come back as text only. Nothing is downloaded.",
                },
            ],
        },
        "stt": {
            "model": machine["stt_model"],
            "size_mb": whisper_mb,
            "hint": (
                "Lets you talk to it. Downloads "
                + (f"{whisper_mb} MB" if whisper_mb else "a speech-recognition model")
                + " during setup, so it works the first time you speak instead "
                "of stalling."
            ),
            "off_hint": "Type only. Nothing is downloaded.",
        },
    }


def build_manifest() -> dict[str, Any]:
    """Everything the form needs, for this machine, at this version."""
    from tesseract.capability.models import DOWNLOAD_LABELS, download_sizes_mb
    from tesseract.config_seed import ensure_config_seeded

    # First, and outside every guard below it: nothing here can be read until
    # `home/config/` exists, and this is the first moment in the install where
    # it can be created.
    ensure_config_seeded()

    machine = _machine()
    sizes = download_sizes_mb(stt_model=machine["stt_model"])
    return {
        "machine": machine,
        "speech": _speech(machine, sizes),
        "extras": [
            {
                "id": key,
                "name": DOWNLOAD_LABELS[key],
                "hint": hint,
                "size_mb": sizes.get(key),
            }
            for key, hint in _EXTRA_ROWS
        ],
        "keys": _key_rows(),
    }


#: The prefix the shell finds this line by, mirroring `provision.rs`'s
#: `MANIFEST_MARKER`.
#:
#: Marked rather than assumed to be the whole stream. stdout is shared with
#: every library this script imports, and a single vendor banner printed there
#: — pynvml's, a deprecation notice, anything — would prefix the JSON and cost
#: the operator the entire form on a machine that is working perfectly.
MARKER = "TESSERACT_SETUP_MANIFEST "


def main() -> int:
    """One marked line of JSON on stdout, diagnostics on stderr.

    Exits non-zero when the manifest could not be built at all, which the shell
    reads the way it reads a setup window that would not open: install the app,
    ask nobody, and leave the optional downloads to Settings.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    try:
        manifest = build_manifest()
    except Exception as exc:  # noqa: BLE001
        logger.error("%s could not be built (%s)", _LABEL, exc)
        return 1
    # One line, no indentation: the shell reads the LAST marked line, so a
    # pretty-printed manifest would arrive as one marked line and a hundred
    # unmarked ones.
    sys.stdout.write(f"{MARKER}{json.dumps(manifest)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
