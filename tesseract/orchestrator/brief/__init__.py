"""Daily-brief orchestration.

Stitches the digester sub-agents in ``tesseract/agents/`` into one
operator-readable markdown file at
``memory-store/daily/briefs/<iso-date>.md``. Wired by:

  * ``/brief`` REPL slash → ``BriefRenderTool`` (synchronous overwrite).
  * the `brief_render` stage → the nightly anchor, idempotent skip if file
    exists.
"""
