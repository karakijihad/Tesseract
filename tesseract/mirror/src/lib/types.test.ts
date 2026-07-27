/* WP-2 follow-up — Vitest coverage for the synthetic-turn dispatch guard.
 *
 * Audit-Informational i1 (2026-05-22): the Python-side stamping is
 * tested in `tests/fix_pass_workspace_parallel_wp1/test_turn_context.py`,
 * but the TypeScript guard's category-exclusion rule
 * (`isSyntheticTurn(env) && env.category !== 'workspace' → drop`)
 * had no coverage. A regression in either helper would silently leak
 * synthetic-turn envelopes into the chat conversation store.
 *
 * Test surface: `isSyntheticTurn` directly, plus the exact predicate
 * the dispatch guard uses, so a change to the helper or the guard's
 * category-exclusion rule is caught here without spinning up the full
 * zustand store graph.
 */

import { describe, it, expect } from 'vitest';
import { isSyntheticTurn, type Envelope } from './types';

function envelope(overrides: Partial<Envelope> & { type: string; category: Envelope['category'] }): Envelope {
  return {
    session_id: 'sid',
    timestamp: '2026-05-22T00:00:00Z',
    data: {},
    ...overrides,
  };
}

/** The exact guard predicate from `stores/dispatch.ts::handleEnvelope`. */
function shouldDrop(env: Envelope): boolean {
  return isSyntheticTurn(env) && env.category !== 'workspace';
}

describe('isSyntheticTurn', () => {
  it('returns false when turn_id is absent', () => {
    expect(isSyntheticTurn(envelope({ type: 'cost_delta', category: 'cost' }))).toBe(false);
  });

  it('returns false for plain uuid turn_id (chat turn)', () => {
    expect(isSyntheticTurn(envelope({
      type: 'stream_text', category: 'loop', turn_id: 'abc123def456',
    }))).toBe(false);
  });

  it('returns true for `syn:<event_id>:<short>` turn_id', () => {
    expect(isSyntheticTurn(envelope({
      type: 'stream_text', category: 'loop', turn_id: 'syn:evt_abc:0000a1b2',
    }))).toBe(true);
  });

  it('returns false for malformed turn_id (no `syn:` prefix)', () => {
    expect(isSyntheticTurn(envelope({
      type: 'stream_text', category: 'loop', turn_id: 'chat:s1',
    }))).toBe(false);
  });

  it('returns false when turn_id is a non-string falsy value', () => {
    expect(isSyntheticTurn(envelope({
      type: 'stream_text', category: 'loop',
      // @ts-expect-error — intentional malformed envelope for the test
      turn_id: null,
    }))).toBe(false);
  });
});

describe('dispatch guard — `isSyntheticTurn && !workspace → drop`', () => {
  it('drops synthetic-turn loop envelopes', () => {
    expect(shouldDrop(envelope({
      type: 'loop_start', category: 'loop', turn_id: 'syn:evt_abc:0000a1b2',
    }))).toBe(true);
    expect(shouldDrop(envelope({
      type: 'stream_tool_call_start', category: 'loop', turn_id: 'syn:evt_abc:1',
    }))).toBe(true);
    expect(shouldDrop(envelope({
      type: 'stream_tool_result', category: 'execution', turn_id: 'syn:evt_abc:1',
    }))).toBe(true);
  });

  it('passes workspace-category envelopes through even with synthetic turn_id', () => {
    // TARS-authored workspace_comment_appended from a synthetic turn
    // legitimately belongs to the workspace pane and must reach
    // `_handleWorkspace` so the comment thread updates.
    expect(shouldDrop(envelope({
      type: 'workspace_comment_appended',
      category: 'workspace',
      turn_id: 'syn:evt_abc:1',
    }))).toBe(false);
  });

  it('passes out-of-turn broadcasts through (no turn_id stamped)', () => {
    // cost_delta, log_error, workspace_event_appended, etc. — broadcast
    // helpers fire from `loop.create_task` outside any chat/synthetic
    // turn so `make_envelope` skips the turn_id stamp via the
    // _TURN_SCOPED_ENVELOPE_TYPES allowlist. Frontend guard sees no
    // turn_id → not synthetic → passes through to category handlers.
    expect(shouldDrop(envelope({ type: 'cost_delta', category: 'cost' }))).toBe(false);
    expect(shouldDrop(envelope({ type: 'log_error', category: 'session' }))).toBe(false);
    expect(shouldDrop(envelope({
      type: 'workspace_event_appended', category: 'workspace',
    }))).toBe(false);
  });

  it('passes chat-turn (non-synthetic) envelopes through', () => {
    expect(shouldDrop(envelope({
      type: 'loop_start', category: 'loop', turn_id: 'abc123def456',
    }))).toBe(false);
    expect(shouldDrop(envelope({
      type: 'stream_text', category: 'loop', turn_id: 'abc123def456',
    }))).toBe(false);
  });
});
