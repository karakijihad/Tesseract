// ASK-over-MCP operator approvals. When an external CLI (Claude Code / Codex)
// calls a write verb, the held tools/call awaits the operator here — approve or
// reject with one click instead of curling /api/mcp/approvals. Polls (approvals
// are rare + the hold window is short); self-hides when nothing is pending.

import { useEffect, useState } from 'react';

import { decideMcpApproval, getMcpApprovals, type McpApproval } from '../lib/api';
import { useWebSocketStore } from '../stores/websocket';
import { Button } from '../components/common/Button';

const POLL_MS = 2500;

export function McpApprovalsPane() {
  const [items, setItems] = useState<McpApproval[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  // The route is operator-session-gated, so no session means no poll rather
  // than a poll that 401s every 2.5s.
  const sessionId = useWebSocketStore((s) => s.sessionId);

  useEffect(() => {
    if (!sessionId) {
      setItems([]);
      return;
    }
    let alive = true;
    const tick = async () => {
      const next = await getMcpApprovals(sessionId);
      if (alive) setItems(next);
    };
    void tick();
    const t = window.setInterval(() => void tick(), POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(t);
    };
  }, [sessionId]);

  async function decide(approvalId: string, approved: boolean) {
    if (busy || !sessionId) return;
    setBusy(approvalId);
    try {
      await decideMcpApproval(approvalId, approved, sessionId);
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
            <Button
              onClick={() => void decide(a.approval_id, true)}
              disabled={!!busy}
              tone="good"
            >
              Approve
            </Button>
            <Button
              onClick={() => void decide(a.approval_id, false)}
              disabled={!!busy}
              tone="danger"
            >
              Reject
            </Button>
          </div>
        </div>
      ))}
    </div>
  );
}
