"""Cross-encoder reranker — the precision stage after RRF fusion.

BM25 and the bi-encoder embed query and document separately; neither ever
reads them together, so the fused candidate order is recall-grade, not
precision-grade. This module re-scores the fused top candidates with a small
ONNX cross-encoder that attends over each (query, text) pair jointly, on CPU
via the onnxruntime that already ships transitively with the local TTS stack.

Model, tokenizer, and limits are injected from the providers catalog entry
that ``roles.yaml::reranker.primary`` points at — this module carries no
model names. Strictly best-effort: missing files, a missing import, or an
inference error all surface as ``rerank() -> None`` and the caller keeps the
RRF order.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


class CrossEncoderReranker:
    def __init__(
        self,
        *,
        model_path: Path,
        tokenizer_path: Path,
        max_seq_len: int,
        candidate_cap: int,
    ) -> None:
        self._model_path = model_path
        self._tokenizer_path = tokenizer_path
        self._max_seq_len = max_seq_len
        self._candidate_cap = candidate_cap
        self._session = None
        self._tokenizer = None
        self._input_names: list[str] = []
        self._load_failed = False
        self._logged_missing = False
        self._lock = threading.Lock()
        # While true, a retrieval will NOT build the session itself; it keeps
        # RRF order and leaves the load to the scheduled warm-up. See
        # `defer_until_warm`.
        self._defer_lazy_load = False

    @property
    def available(self) -> bool:
        if self._load_failed:
            return False
        if self._session is not None:
            return True
        present = self._model_path.exists() and self._tokenizer_path.exists()
        if not present and not self._logged_missing:
            self._logged_missing = True
            logger.info(
                "reranker model files absent (%s) — retrieval keeps RRF order; "
                "run scripts/fetch_reranker_model.py to enable",
                self._model_path.parent,
            )
        return present

    def _ensure_loaded(self) -> bool:
        """Load session + tokenizer once, under the lock. Sync — thread only."""
        with self._lock:
            if self._session is not None:
                return True
            if self._load_failed:
                return False
            try:
                import onnxruntime  # type: ignore[import-untyped]
                from tokenizers import Tokenizer  # type: ignore[import-untyped]

                tokenizer = Tokenizer.from_file(str(self._tokenizer_path))
                tokenizer.enable_truncation(self._max_seq_len)
                tokenizer.enable_padding()
                opts = onnxruntime.SessionOptions()
                opts.log_severity_level = 3
                session = onnxruntime.InferenceSession(
                    str(self._model_path),
                    sess_options=opts,
                    providers=["CPUExecutionProvider"],
                )
                self._input_names = [i.name for i in session.get_inputs()]
                self._tokenizer = tokenizer
                self._session = session
                logger.info("reranker loaded (%s)", self._model_path.name)
                return True
            except Exception as exc:
                self._load_failed = True
                logger.warning("reranker load failed — retrieval keeps RRF order: %s", exc)
                return False

    async def warm_up(self) -> None:
        """Load session + tokenizer off the loop so the first retrieval
        doesn't pay the cold-load tax (~seconds). No-op when unavailable.

        Clears any `defer_until_warm` hold in a `finally`, so a warm-up that
        FAILED does not leave retrieval permanently refusing to rerank: the
        deferral exists to move one blocking load off the boot path, not to
        disable the stage. After this returns, the lazy path is available
        again as the backstop it was.
        """
        if not self.available:
            return
        try:
            loaded = await asyncio.to_thread(self._ensure_loaded)
        finally:
            self._defer_lazy_load = False
        if loaded:
            logger.info("reranker warmed")

    def defer_until_warm(self) -> None:
        """Refuse to build the session on a retrieval until warm-up has run.

        Building an onnxruntime session holds the GIL for its whole duration
        — measured at several seconds — so it blocks the event loop even from
        a worker thread. That is acceptable once, in the warm-up, which is
        scheduled after the backend is answering. It is not acceptable on the
        retrieval path during boot, and that is reachable: the autonomy
        kernel runs a resume sweep while boot is still finishing, and its
        retrieval would build the session first, beating the warm-up to it
        and stalling startup by seconds.

        Skipping costs the ordering quality of those first few retrievals,
        nothing more — the caller keeps the RRF order, which is this module's
        documented degraded mode and already its behaviour when the model
        files are absent. Recall-grade ordering on an autonomy sweep is a
        smaller loss than a backend that answers nothing for six seconds.
        """
        self._defer_lazy_load = True

    async def rerank(
        self, query: str, candidates: list[tuple[str, str]]
    ) -> list[tuple[str, float]] | None:
        """Score (query, text) pairs. Returns [(id, score 0..1)] best-first,
        or None when unavailable/failed — caller keeps its existing order."""
        if not candidates or not self.available:
            return None
        if self._defer_lazy_load and self._session is None:
            logger.debug("reranker not warmed yet — keeping RRF order for this query")
            return None
        pool = candidates[: self._candidate_cap]
        return await asyncio.to_thread(self._rerank_sync, query, pool)

    def _rerank_sync(
        self, query: str, candidates: list[tuple[str, str]]
    ) -> list[tuple[str, float]] | None:
        if not self._ensure_loaded():
            return None
        try:
            encodings = self._tokenizer.encode_batch(
                [(query, text) for _cid, text in candidates]
            )
            feeds: dict[str, np.ndarray] = {}
            if "input_ids" in self._input_names:
                feeds["input_ids"] = np.array(
                    [e.ids for e in encodings], dtype=np.int64
                )
            if "attention_mask" in self._input_names:
                feeds["attention_mask"] = np.array(
                    [e.attention_mask for e in encodings], dtype=np.int64
                )
            if "token_type_ids" in self._input_names:
                feeds["token_type_ids"] = np.array(
                    [e.type_ids for e in encodings], dtype=np.int64
                )
            logits = self._session.run(None, feeds)[0].reshape(-1).astype(np.float64)
            scores = 1.0 / (1.0 + np.exp(-logits))
            ranked = [
                (cid, float(score))
                for (cid, _text), score in zip(candidates, scores)
            ]
            ranked.sort(key=lambda x: x[1], reverse=True)
            return ranked
        except Exception as exc:
            logger.warning("rerank inference failed — keeping RRF order: %s", exc)
            return None


__all__ = ["CrossEncoderReranker"]
