// GENERATED — do not hand-edit. Source of truth:
// `tesseract/workspace_events/events.py::ANSWERABLE_WITH`.
//
// What a workspace card kind can be answered with: 'approve'/'reject' for a
// decision, 'resolve' for a report. Regenerate after `ANSWERABLE_WITH`
// changes:
//   python -m tesseract.scripts.generate_workspace_kinds --write
// Check for drift:
//   python -m tesseract.scripts.generate_workspace_kinds --check

import type { EventKind } from './workspace';

export const ANSWERABLE_WITH: Record<EventKind, readonly string[]> = {
  feedback_proposal: ['approve', 'reject'],
  feedback_sweep: ['approve', 'reject'],
  agent_approval: ['approve', 'reject'],
  soul_proposal: ['approve', 'reject'],
  change_proposal: ['approve', 'reject'],
  mission_reflection_proposal: [],
  reflection_proposal: ['resolve'],
  nudge: ['approve', 'reject', 'resolve'],
  agent_post: ['resolve'],
  operator_post: ['resolve'],
  daily_brief: ['resolve'],
  yaml_change_proposal: ['approve', 'reject'],
  kb_merge_conflict: ['approve', 'reject'],
  recovery_summary: ['resolve'],
  vault_raw_ingest_batch: ['approve', 'reject'],
  clarification: ['approve', 'reject', 'resolve'],
  strategist_summary: ['resolve'],
  runtime_lock_deny: ['resolve'],
  skill_approval: ['approve', 'reject'],
  skill_refinement: ['approve', 'reject'],
  skill_retirement: ['approve', 'reject'],
  working_set_proposal: ['approve', 'reject'],
  tuning_proposal: ['approve', 'reject'],
  project_proposal: ['approve', 'reject'],
};

/** Whether the Inbox draws Approve/Reject for a card of this kind, rather
 *  than a plain Resolve. `resolve` wins when a kind carries both:
 *  `clarification` and `nudge` are backend-decidable (they count as
 *  "waiting on you" for `/queue`, the notifier, the return note), but the
 *  operator's real answer is a comment and the row draws only Resolve —
 *  `orchestrator/recovery/effects.py::build_card` files a `clarification`
 *  specifically because it "does not offer a button that runs anything".
 *  A kind that is only decidable (no `resolve` in its verbs) draws
 *  Approve/Reject as it always has. */
export function isActionable(kind: EventKind): boolean {
  const verbs = ANSWERABLE_WITH[kind] ?? [];
  return verbs.includes('approve') && !verbs.includes('resolve');
}
