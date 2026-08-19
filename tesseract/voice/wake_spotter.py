"""The wake phrase, spotted in audio by a decoder that can hear nothing else.

A tiny transducer whose decoding graph is **restricted to one phrase**. It is
a speech recogniser in shape, but it can only ever produce the wake words —
ordinary speech decodes to nothing at all, so there is no transcript here to
leak, log or match against.

This replaces a few-shot embedding matcher that was built, measured against a
real recording, and rejected. The reason it could not be rescued is worth
keeping where the replacement lives: cosine distance to a centroid learns a
centre and never a boundary, so nothing in it ever expressed what *not* to
fire on, and silence embeds to the same place whatever preceded it. A decoder
constrained to a token sequence has the boundary built in.

Two properties the rest of the voice path depends on:

- **No training, ever.** A custom phrase is compiled to BPE tokens from the
  model's own vocabulary at the moment the name changes. There is no
  enrollment step and no per-phrase model.
- **A binary verdict.** The decoder emits the keyword or it emits nothing;
  there is no confidence number to surface, threshold against afterwards, or
  show the operator. Sensitivity is set *before* decoding, by the two
  parameters the spotter is constructed with.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from tesseract.paths import runtime_dir

log = logging.getLogger(__name__)

#: The int8 trio plus the two vocabulary files. The fp32 graphs in the same
#: archive are 8.6 MB more for a gate that answers yes or no, so the fetch
#: keeps these five and discards the rest.
ENCODER_FILE = "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
DECODER_FILE = "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
JOINER_FILE = "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
TOKENS_FILE = "tokens.txt"
BPE_FILE = "bpe.model"

MODEL_FILES = (ENCODER_FILE, DECODER_FILE, JOINER_FILE, TOKENS_FILE, BPE_FILE)

#: The keywords file sherpa reads at construction. One phrase, rewritten
#: whenever the name or prefix changes. On disk rather than in a temporary
#: file so that what the gate is actually listening for is inspectable —
#: "it does not hear me" is otherwise unanswerable.
_KEYWORDS_FILENAME = "wake-keywords.txt"

#: Trailing silence appended to every utterance before decoding. The decoder
#: emits on trailing blanks, so a phrase spoken at the very end of the buffer
#: is still being decoded when the samples run out — measured: without this,
#: a take that ends on the wake word does not fire.
_TAIL_SECONDS = 0.5

SAMPLE_RATE = 16_000


class WakeModelsUnavailable(RuntimeError):
    """The pinned model is not on disk, or would not load.

    A distinct type because the caller's response is not an error path: the
    gate fails open to take-everything behaviour, exactly as it does for a
    missing config. A wake word that cannot load must never become a mute.
    """


class WakeUndecidable(RuntimeError):
    """This utterance could not be decided — bad audio, or a decoder fault.

    Separate from "did not fire", and both callers must treat it as fail-open.
    Reporting it as a miss would quietly convert "I cannot tell" into "that was
    not the wake word", which is a mute.
    """


class PhraseUnspottable(RuntimeError):
    """The phrase cannot be built from this model's vocabulary.

    Its own type because it is neither a fault nor a miss: the configuration
    is the problem, it will not fix itself, and the operator has to be told
    which name cannot be heard. Until then the gate passes everything.
    """


def models_dir() -> Path:
    from tesseract.voice import model_files

    return model_files.lane_dir("wake")


def models_present(target_dir: Path | None = None) -> bool:
    """Cheap five-stat check — no network, no ONNX load.

    What Settings renders from, and what decides whether the gate can arm.
    """
    root = target_dir or models_dir()
    return all((root / name).is_file() for name in MODEL_FILES)


def keywords_path() -> Path:
    return runtime_dir() / _KEYWORDS_FILENAME


def compile_phrase(phrase: str, target_dir: Path | None = None) -> list[str]:
    """The phrase as BPE pieces from the model's own vocabulary.

    Deliberately not ``sherpa_onnx.text2token``: that helper imports
    ``pypinyin`` unconditionally, a Chinese-only dependency the BPE branch
    never touches. This is that branch, and it is checked against the
    publisher's own bundled keyword list.

    Raises `PhraseUnspottable` when a piece is outside the model's token
    table. That is a real outcome for an unusual name, and it must surface as
    a stated reason rather than as a wake word that silently never fires.
    """
    import sentencepiece as spm

    root = target_dir or models_dir()
    text = " ".join(phrase.split()).upper()
    if not text:
        raise PhraseUnspottable("the wake phrase is empty")

    processor = spm.SentencePieceProcessor()
    processor.load(str(root / BPE_FILE))
    pieces = processor.encode(text, out_type=str)

    table = {
        line.split()[0]
        for line in (root / TOKENS_FILE).read_text(encoding="utf-8").splitlines()
        if line.split()
    }
    unknown = [p for p in pieces if p not in table]
    if unknown:
        raise PhraseUnspottable(
            f"{phrase!r} cannot be built from this model's vocabulary "
            f"({', '.join(unknown)}) — the wake word needs a different name"
        )
    return pieces


def write_keywords(phrase: str, target_dir: Path | None = None) -> Path:
    """Compile the phrase and put it where the decoder reads it."""
    pieces = compile_phrase(phrase, target_dir)
    path = keywords_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(" ".join(pieces) + "\n", encoding="utf-8")
    return path


@dataclass(frozen=True)
class SpotterKey:
    """What a loaded spotter is specific to.

    All three are baked in at construction — sherpa takes the keywords file
    and both sensitivity parameters as constructor arguments — so a change to
    any of them means a new spotter rather than a new call.
    """

    phrase: str
    threshold: float
    boost: float


class WakeSpotter:
    """One loaded decoder, reused for every utterance.

    Not built at import: the model may be absent on a first run and arrive
    later without a restart, so construction is the caller's decision and
    absence is re-checked cheaply.
    """

    def __init__(self, key: SpotterKey, target_dir: Path | None = None) -> None:
        import sherpa_onnx

        root = target_dir or models_dir()
        if not models_present(root):
            raise WakeModelsUnavailable("wake model files are not installed")
        keywords = write_keywords(key.phrase, root)
        self.key = key
        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(root / TOKENS_FILE),
            encoder=str(root / ENCODER_FILE),
            decoder=str(root / DECODER_FILE),
            joiner=str(root / JOINER_FILE),
            keywords_file=str(keywords),
            keywords_threshold=key.threshold,
            keywords_score=key.boost,
            num_threads=1,
            provider="cpu",
        )

    def spot(self, pcm: bytes) -> bool:
        """Whether the wake phrase is in this complete utterance.

        ``pcm`` is int16 LE mono at 16 kHz — the shape the session already
        buffers, so nothing is resampled.

        The one-shot form, kept for the confirmation run in Settings, which
        genuinely has whole recordings in hand. The live path streams instead
        (`new_stream` / `feed`), because deciding only once the operator has
        stopped talking is what made a refusal arrive a minute late.
        """
        stream = self.new_stream()
        if self.feed(stream, pcm):
            return True
        # Trailing silence: the decoder emits on trailing blanks, so a phrase
        # at the very end of the buffer is still mid-decode when the samples
        # run out. Measured — without this a take that ends on the wake word
        # does not fire.
        tail = np.zeros(int(_TAIL_SECONDS * SAMPLE_RATE), dtype=np.float32)
        stream.accept_waveform(SAMPLE_RATE, tail)
        stream.input_finished()
        return self._drain(stream)

    def new_stream(self) -> Any:
        """A decoding stream for one utterance. 0.33 ms — measured."""
        return self._spotter.create_stream()

    def feed(self, stream: Any, pcm: bytes) -> bool:
        """Push one frame of live audio; True the moment the phrase lands.

        Cheap enough to run on the event loop: 3.46 ms for the 100 ms frames
        the capture path sends, about 3.5% of realtime — measured, and well
        inside the 50 ms a single step may block for. Handing each frame to a
        worker thread would spend more on the hop than on the decode.
        """
        if len(pcm) < 2:
            raise WakeUndecidable(f"{len(pcm)} bytes is not audio")
        samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16)
        stream.accept_waveform(SAMPLE_RATE, samples.astype(np.float32) / 32768.0)
        return self._drain(stream)

    def _drain(self, stream: Any) -> bool:
        while self._spotter.is_ready(stream):
            self._spotter.decode_stream(stream)
            if self._spotter.get_result(stream).strip():
                # The keywords file holds exactly one phrase, so any hit is
                # the phrase. Stop on the first — a second occurrence in the
                # same utterance is the same answer.
                return True
        return False


def load_spotter(key: SpotterKey, target_dir: Path | None = None) -> Any | None:
    """Build a spotter, or None with the reason logged.

    Never raises for an environment problem — a missing package, an absent
    model and a corrupt graph all reach the same fail-open outcome, and this
    runs on the voice path where an exception is a dead microphone.
    `PhraseUnspottable` is deliberately NOT swallowed: it is a configuration
    fault the operator can fix and must be told about.
    """
    try:
        return WakeSpotter(key, target_dir)
    except PhraseUnspottable:
        raise
    except WakeModelsUnavailable as exc:
        log.warning("wake_word: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 - any load failure is the same outcome
        log.warning("wake_word: could not load the spotter (%s)", exc)
        return None
