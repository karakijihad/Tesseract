// ASK-over-MCP operator approvals. When an external CLI (Claude Code / Codex)
// calls a write verb, the held tools/call awaits the operator here — approve or
// reject with one click instead of curling /api/mcp/approvals. Polls (approvals
// are rare + the hold window is short); self-hides when nothing is pending.

import { useEffect, useState } from 'react';

import { decideMcpApproval, getMcpApprovals, type McpApproval } from '../lib/api';

const POLL_MS = 2500;

export function McpApprovalsPane() {
  const [items, setItems] = useState<McpApproval[]>([]);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      const next = await getMcpApprovals();
      if (alive) setItems(next);
    };
    void tick();
    const t = window.setInterval(() => void tick(), POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(t);
    };
  }, []);

  async function decide(approvalId: string, approved: boolean) {
    if (busy) return;
    setBusy(approvalId);
    try {
      await decideMcpApproval(approvalId, approved);
      setItems((prev) => prev.filter((x) => x.approval_id !== approvalId)); // optimistic; poll re-syncs
    } catch {
      // leave it in the list — the next poll reflects the true state
    } finally {
      setBusy(null);
    }
  }

  if (items.length === 0) return null;

  return (
    <div className="mcp-approvals" role="alertdialog" aria-label="MCP approvals">
      <div className="mcp-approvals__title">
        MCP approval{items.length > 1 ? `s (${items.length})` : ''}
      </div>
      {items.map((a) => (
        <div key={a.approval_id} className="mcp-approvals__row">
          <div className="mcp-approvals__what">
            <span className="mcp-approvals__verb">{a.verb}</span>
            <span className="mcp-approvals__client t-meta">{a.client}</span>
          </div>
          <div className="mcp-approvals__actions">
            <button
              type="button"
              className="mcp-approvals__approve"
              disabled={!!busy}
              onClick={() => void decide(a.approval_id, true)}
            >
              Approve
            </button>
            <button
              type="button"
              className="mcp-approvals__reject"
              disabled={!!busy}
              onClick={() => void decide(a.approval_id, false)}
            >
              Reject
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
