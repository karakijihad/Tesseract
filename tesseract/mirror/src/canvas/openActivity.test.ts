import { describe, it, expect, beforeEach, vi } from 'vitest';
import { openActivity } from './openActivity';
import * as dt from './delegateTranscript';
import type { ActivityRecord } from '../stores/activity';

function rec(kind: string, id: string, extra: Partial<ActivityRecord> = {}): ActivityRecord {
  return {
    activity_id: `${kind === 'controller_session' ? 'session' : kind}:${id}`,
    kind, label: 'x', state: 'running', durability: 'persistent', provider: 'claude',
    parent_turn_id: null, parent_session_id: null, transcript_ref: null,
    started_at: '', updated_at: '', ...extra,
  };
}

describe('openActivity', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('routes a delegate to openDelegateTranscript with the bare id', async () => {
    const spy = vi.spyOn(dt, 'openDelegateTranscript').mockResolvedValue();
    await openActivity(rec('delegate', 'abc', { label: 'claude' }));
    expect(spy).toHaveBeenCalledWith('abc', 'claude');
  });

  it('routes a lane to a POST /api/surfaces lane descriptor', async () => {
    const fetchSpy = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ surfaces: [] }) }) // dedupe check
      .mockResolvedValueOnce({ ok: true, json: async () => ({}) });             // create
    vi.stubGlobal('fetch', fetchSpy);
    await openActivity(rec('lane', 'L1'));
    const createCall = fetchSpy.mock.calls[1];
    expect(createCall[1].method).toBe('POST');
    expect(JSON.parse(createCall[1].body).type).toBe('lane');
    expect(JSON.parse(createCall[1].body).props.lane_id).toBe('L1');
    vi.unstubAllGlobals();
  });

  it('routes a controller_session to a POST /api/surfaces session-transcript descriptor', async () => {
    const fetchSpy = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ surfaces: [] }) }) // dedupe check
      .mockResolvedValueOnce({ ok: true, json: async () => ({}) });             // create
    vi.stubGlobal('fetch', fetchSpy);
    await openActivity(rec('controller_session', 'S9', { label: 'chat' }));
    const createCall = fetchSpy.mock.calls[1];
    expect(createCall[1].method).toBe('POST');
    const body = JSON.parse(createCall[1].body);
    expect(body.type).toBe('session-transcript');
    expect(body.props.session_id).toBe('S9');
    vi.unstubAllGlobals();
  });

  it('resolves without throwing or warning on a kind with no surface (detail block lives in ActivityMap)', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    await expect(openActivity(rec('routine', 'r1'))).resolves.toBeUndefined();
    expect(warn).not.toHaveBeenCalled();
  });
});
