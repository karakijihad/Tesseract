"""TESSERACT memory subsystem.

Canonical file-first memory with involuntary kernel hooks and voluntary tools.
"""

from tesseract.memory.consistency import ConsistencyChecker, ConsistencyReport
from tesseract.memory.dreaming import DreamingEngine
from tesseract.memory.embeddings import EmbeddingIndex
from tesseract.memory.fts_index import FTSIndex
from tesseract.memory.index import MemoryIndex
from tesseract.memory.retrieval import RetrievalPipeline, RetrievalResult
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType, Stability
from tesseract.memory.what_not_to_save import WhatNotToSave

__all__ = [
    "ConsistencyChecker",
    "ConsistencyReport",
    "DreamingEngine",
    "EmbeddingIndex",
    "FTSIndex",
    "MemoryFrontmatter",
    "MemoryIndex",
    "MemoryStore",
    "MemoryType",
    "RetrievalPipeline",
    "RetrievalResult",
    "Stability",
    "WhatNotToSave",
]
