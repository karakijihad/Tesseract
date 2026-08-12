"""Daily-brief orchestration — MO-9-8 → MO-9-13.

Stitches the six markdown agents in ``tesseract/agents/daily-brief.md``
plus five digesters into one operator-readable markdown file at
``memory-store/daily/briefs/<iso-date>.md``. Wired by:

  * ``/brief`` REPL slash → ``BriefRenderTool`` (synchronous overwrite).
  * ``DailyBriefJob`` (cron) → daily 08:00, idempotent skip if file
    exists.

The world section is sourced from three fixed pillars
(:data:`tesseract.orchestrator.brief.pillars.DEFAULT_PILLARS`) and
ranked by the operator's
:class:`~tesseract.orchestrator.brief.interests.InterestsProfile`. The
MO-9-8 ``tracked-topics.yaml`` curated-list path is retired (loader
left in place for one phase per MO-9-13 §7 plan; MO-9-14 removes the
loader after confirming no orphans).

Contract:
the brief renderer spec below.
"""
