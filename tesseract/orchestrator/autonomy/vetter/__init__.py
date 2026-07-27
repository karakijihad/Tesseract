"""Task 2B — agent-vetter: judges UNVETTED agenda proposals via an LLM role.

``parse.py`` — response schema + lenient JSON parsing.
``prompt.py`` — batched vet-prompt builder.
Job wiring lives in ``tesseract/scheduler/tasks/autonomy_vetter.py``.
"""

from __future__ import annotations
