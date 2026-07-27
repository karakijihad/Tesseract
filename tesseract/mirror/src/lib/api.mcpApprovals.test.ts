// ASK-over-MCP operator-approval api: list pending + decide.

import { afterEach, describe, expect, it, vi } from 'vitest';

import { decideMcpApproval, getMcpApprovals } from './api';

afterEach(() => vi.unstubAllGlobals());

describe('MCP approvals api', () => {
  it('getMcpApprovals returns the items array', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        json: async () => ({ items: [{ approval_id: 'a1', verb: 'lane.ensure', client: 'operator' }] }),
      })),
    );
    expect(await getMcpApprovals()).toEqual([
      { approval_id: 'a1', verb: 'lane.ensure', client: 'operator' },
    ]);
  });

  it('getMcpApprovals returns [] on a non-ok response', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, json: async () => ({}) })));
    expect(await getMcpApprovals()).toEqual([]);
  });

  it('getMcpApprovals returns [] when fetch throws', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('network'); }));
    expect(await getMcpApprovals()).toEqual([]);
  });

  it('decideMcpApproval posts {approved} to the approval id', async () => {
    const spy = vi.fn(async () => ({ ok: true, json: async () => ({ approved: true }) }));
    vi.stubGlobal('fetch', spy);
    await decideMcpApproval('a1', true);
    expect(spy).toHaveBeenCalledWith(
      expect.stringContaining('/api/mcp/approvals/a1/decision'),
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ approved: true }) }),
    );
  });
});
