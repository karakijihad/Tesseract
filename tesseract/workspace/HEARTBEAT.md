---
version: 1
last_updated: null
---

# HEARTBEAT

Scheduled consolidation pass. Runs daily via the scheduler (`LibrarianHeartbeatJob`, default 15:00) and on-demand via Mirror `/reflect`, which refreshes `pending_growth.md` first, then dispatches `reflect_on_session()`, then runs `librarian.run_pass()`.

Each heartbeat is one bounded pass. If context is tight, skip steps — never half-run a step.

## Checklist

- [ ] **Read** today's and yesterday's `memory-store/daily/YYYY-MM-DD.md` — the raw capture layer.
- [ ] **Promote** durable facts from daily → canonical subdirs (`user/` / `feedback/` / `project/` / `reference/`). Criteria: body > 80 chars, not a request-echo, clear subject matter, not already present in canonical store.
- [ ] **Dedupe** recent writes via cosine similarity (0.92 threshold). Near-duplicates update `updated_at` on the existing entry instead of creating a new one.
- [ ] **Refresh** per-subdir `INDEX.md` cross-references so wikilinks stay resolvable.
- [ ] **Synthesize** the top-level `memory-store/MEMORY.md`: top-20 most-retrieved entries + recent promotions. Keep ≤120 lines per doc-size rules.
- [ ] **Distill** the last 7 days of `memory-store/diary/*.md` against SOUL.md `## Growth`, write 0–3 candidate bullets to `memory-store/pending_growth.md`. The librarian only proposes; TARS decides at `/reflect`.
- [ ] **Log** this heartbeat run to today's `daily/YYYY-MM-DD.md` with a one-line summary (`heartbeat: promoted N / deduped M / skipped K / distilled C`).

## Invariants

- **No writes outside the memory-store.** The librarian does not touch `workspace/`, `vault/`, or source code. Personality candidates are written to `memory-store/pending_growth.md`, never to SOUL.md — only `soul_growth_propose` (called by TARS) mutates SOUL.md.
- **Fail open.** If Ollama is offline, skip the dedupe step silently; promotion still runs. If the chat adapter is offline, distillation no-ops without overwriting an existing `pending_growth.md`.
- **One pass, then stop.** Do not loop. Do not run nested reflections.
- **Transparency.** When TARS calls `soul_growth_propose` during `/reflect`, the runtime emits `soul_updated` over WS so the operator sees the change. Silent self-edits are not allowed.
