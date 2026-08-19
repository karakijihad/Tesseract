"""Confirming the wake word works here, and recording what made it work.

The spotter needs no enrollment — it can hear a phrase it has never been
trained on. So this is not "teach it your voice"; it is the operator saying
the phrase, watching it fire, and the sensitivity that made that happen being
written down. Nothing here trains anything, and it is re-runnable without
limit.

It still gates arming, and that is deliberate: until someone has confirmed
the wake word fires for *them*, in *their* room, the gate passes every
utterance through. A shipped sensitivity that suits one voice and not another
would otherwise make that person's install appear to ignore them, with
nothing on screen to explain it.

Two things make this more than "it worked once":

- **The ladder starts strict.** The setting stored is the strictest one at
  which every take fired, not the first one that worked. Reliability bought
  by widening a threshold is the failure this whole feature exists to avoid.
- **A refusal.** Ordinary speech is recorded alongside the takes, and if it
  fires at the same setting, this writes nothing and says so. A wake word
  that triggers on conversation is worse than one that needs repeating.

The artifact is the phrase and two numbers — never the audio.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from tesseract.paths import runtime_dir

log = logging.getLogger(__name__)

_FILENAME = "wake-calibration.json"

#: Tried strictest first, so the stored setting is the tightest one that
#: actually works rather than the first one that does. The range is the one
#: measured against the model's own read-speech samples: every present phrase
#: fired from 0.05 to 0.35 and nothing absent fired at any setting, so the
#: interesting territory is the top of that band, and going below the floor
#: buys nothing a real miss would not also survive.
#:
#: **Four rungs, not six, and the reason is cost rather than taste.** Each rung
#: is a fresh decoder — sherpa bakes the threshold in at construction — and
#: that costs 9-12 s the first time in a process and ~3 s after. Six rungs is
#: half a minute of loading to separate 0.30 from 0.25, a distinction the sweep
#: showed nothing riding on. These four span the same band.
THRESHOLD_LADDER: tuple[float, ...] = (0.45, 0.35, 0.25, 0.15)


class Spotter(Protocol):
    def spot(self, pcm: bytes) -> bool: ...


@dataclass(frozen=True)
class CalibrationReport:
    """What the run observed, whether or not it was stored.

    Carries the counts rather than a verdict alone because the operator is
    owed the reason: "two of your five takes did not fire" and "your normal
    speech fired" are different problems with different fixes.
    """

    ok: bool
    reason: str
    threshold: float
    phrase_hits: int
    phrase_takes: int
    speech_hits: int
    speech_takes: int
    tried: tuple[float, ...]


@dataclass(frozen=True)
class WakeCalibration:
    """The stored setting. `phrase` is what it was confirmed for.

    The phrase is kept so a rename can invalidate it: what was confirmed is
    that two particular words are heard reliably, and continuing to trust it
    after the assistant is renamed would claim a confirmation nobody made.
    """

    threshold: float
    boost: float
    phrase: str
    samples: int

    def matches_phrase(self, phrase: str) -> bool:
        return self.phrase.strip().casefold() == (phrase or "").strip().casefold()


def calibration_path() -> Path:
    return runtime_dir() / _FILENAME


def calibrate(
    positives: Sequence[bytes],
    negatives: Sequence[bytes],
    *,
    phrase: str,
    boost: float,
    make_spotter: Callable[[float], Spotter],
) -> tuple[WakeCalibration | None, CalibrationReport]:
    """Takes of the phrase in, a confirmed setting or a refusal out.

    ``positives`` are recordings of the phrase, ``negatives`` a few seconds of
    ordinary speech. Both are raw PCM; nothing is written to disk.

    ``make_spotter`` builds a spotter at a given threshold. Injected rather
    than constructed here so the ladder can be exercised without loading an
    ONNX graph six times.
    """
    usable = [clip for clip in positives if clip]
    if not usable:
        return None, CalibrationReport(
            ok=False,
            reason="no usable recordings of the phrase",
            threshold=0.0,
            phrase_hits=0,
            phrase_takes=0,
            speech_hits=0,
            speech_takes=0,
            tried=(),
        )

    speech = [clip for clip in negatives if clip]
    if not speech:
        # Without them there is nothing to refuse against: any setting that
        # fires on every take would pass, including one that also fires on
        # conversation. That is exactly the unvalidated sensitivity this
        # exists to catch, and it would look validated.
        return None, CalibrationReport(
            ok=False,
            reason=(
                "need a few seconds of ordinary speech as well — without it "
                "there is nothing to check the phrase against, and a setting "
                "that was never tested against normal talking is one that will "
                "fire during it."
            ),
            threshold=0.0,
            phrase_hits=0,
            phrase_takes=len(usable),
            speech_hits=0,
            speech_takes=0,
            tried=(),
        )

    tried: list[float] = []
    best_hits = 0
    for threshold in THRESHOLD_LADDER:
        tried.append(threshold)
        spotter = make_spotter(threshold)
        hits = sum(1 for clip in usable if spotter.spot(clip))
        best_hits = max(best_hits, hits)
        if hits < len(usable):
            continue

        # Strictest setting that hears every take. Nothing looser can make
        # the ordinary speech quieter, so this is the one place to check it.
        false_hits = sum(1 for clip in speech if spotter.spot(clip))
        if false_hits:
            return None, CalibrationReport(
                ok=False,
                reason=(
                    f"the phrase was heard in all {len(usable)} takes at "
                    f"{threshold:.2f}, but ordinary speech fired "
                    f"{false_hits} time{'s' if false_hits > 1 else ''} at the "
                    "same setting. A name that sounds like something you say "
                    "anyway cannot be gated apart from it — try a more "
                    "distinctive one."
                ),
                threshold=threshold,
                phrase_hits=hits,
                phrase_takes=len(usable),
                speech_hits=false_hits,
                speech_takes=len(speech),
                tried=tuple(tried),
            )

        return (
            WakeCalibration(
                threshold=float(threshold),
                boost=float(boost),
                phrase=phrase.strip(),
                samples=len(usable),
            ),
            CalibrationReport(
                ok=True,
                reason=(
                    f"heard in all {len(usable)} takes at {threshold:.2f}, and "
                    f"not once in {len(speech)} recordings of ordinary speech"
                ),
                threshold=float(threshold),
                phrase_hits=hits,
                phrase_takes=len(usable),
                speech_hits=0,
                speech_takes=len(speech),
                tried=tuple(tried),
            ),
        )

    return None, CalibrationReport(
        ok=False,
        reason=(
            f"the phrase was heard in {best_hits} of {len(usable)} takes, even at "
            f"the most sensitive setting this gate will use ({THRESHOLD_LADDER[-1]:.2f}). "
            "Record where you actually use it, saying the phrase the way you "
            "normally would — and check the microphone is the one you speak into."
        ),
        threshold=0.0,
        phrase_hits=best_hits,
        phrase_takes=len(usable),
        speech_hits=0,
        speech_takes=len(speech),
        tried=tuple(tried),
    )


def save(calibration: WakeCalibration, path: Path | None = None) -> Path:
    """Write the setting atomically.

    Atomically because the gate reads this file on a path where a truncated
    read means the wake word silently stops working — and the operator's next
    move would be to re-run this, which rewrites the same file.
    """
    target = path or calibration_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        # Bumped from 1, which stored a reference vector for the rejected
        # embedding matcher. `load` refuses anything it does not recognise, so
        # an old file disarms the gate rather than being half-read.
        "version": 2,
        "phrase": calibration.phrase,
        "threshold": calibration.threshold,
        "boost": calibration.boost,
        "samples": calibration.samples,
    }
    tmp = target.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(target)
    except Exception:
        # Leave nothing behind on a failed write. `runtime/` is the tree the
        # janitor sweeps, and an orphaned `.tmp` there is a file a future
        # session has to work out the provenance of.
        tmp.unlink(missing_ok=True)
        raise
    return target


def load(path: Path | None = None) -> WakeCalibration | None:
    """The stored setting, or None with the reason logged.

    Every failure here returns None, which leaves the gate unarmed and every
    utterance dispatching. A calibration that cannot be read must not become a
    gate that rejects everything.
    """
    target = path or calibration_path()
    if not target.is_file():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        version = int(raw.get("version") or 0)
        threshold = float(raw["threshold"])
        boost = float(raw["boost"])
        phrase = str(raw["phrase"])
        # Inside the guard, not after it. `samples` is display metadata and
        # nothing gates on it — which is exactly why parsing it outside was
        # dangerous: a junk value raised out of `load()`, through the gate, and
        # out of the voice handler, killing the turn with no envelope and
        # leaving the orb spinning. A field nobody reads must not be able to
        # take the microphone down.
        samples = int(raw.get("samples") or 0)
    except Exception as exc:  # noqa: BLE001 - any unreadable shape is the same outcome
        log.warning(
            "wake_word: calibration at %s is unreadable (%s) — gate stays open",
            target,
            exc,
        )
        return None
    if version != 2:
        log.warning(
            "wake_word: calibration at %s is version %d, which this gate cannot "
            "read — re-run it from Settings → Voice",
            target,
            version,
        )
        return None
    if not 0.0 < threshold <= 1.0 or not 0.0 < boost <= 10.0:
        log.warning("wake_word: calibration values are out of range — gate stays open")
        return None
    return WakeCalibration(
        threshold=threshold, boost=boost, phrase=phrase, samples=samples
    )


def clear(path: Path | None = None) -> bool:
    """Delete the calibration — the phase's whole rollback story.

    Removing it returns the install to take-everything behaviour with no
    config edit and no migration.
    """
    target = path or calibration_path()
    if not target.is_file():
        return False
    target.unlink()
    return True
