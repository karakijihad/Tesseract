// Parked background-spawn asks (trio W4 — ask-instead-of-die). A spawn's ASK
// that outlived its live 30s card waits here instead of denying; the operator
// approves or denies it from the approvals surface (M3). Polls (parked asks are
// rare); self-hides when nothing is parked. Reuses the `.mcp-approvals` styling;
// `.parked-asks` floats it at the bottom so it never overlaps the MCP pane.

import { useEffect, useState } from "react";

import { decideParkedAsk, getParkedAsks, type ParkedAsk } from "../lib/api";

const POLL_MS = 2500;

export function ParkedAsksPane() {
  const [items, setItems] = useState<ParkedAsk[]>([]);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      const next = await getParkedAsks();
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
      await decideParkedAsk(approvalId, approved);
      setItems((prev) => prev.filter((x) => x.approval_id !== approvalId)); // optimistic; poll re-syncs
    } catch {
      // leave it in the list — the next poll reflects the true state
    } finally {
      setBusy(null);
    }
  }

  if (items.length === 0) return null;

  return (
    <div
      className="mcp-approvals parked-asks"
      role="alertdialog"
      aria-label="Parked asks"
    >
      <div className="mcp-approvals__title">
        Parked ask{items.length > 1 ? `s (${items.length})` : ""}
      </div>
      {items.map((a) => (
        <div key={a.approval_id} className="mcp-approvals__row">
          <div className="mcp-approvals__what">
            <span className="mcp-approvals__verb">{a.tool_name}</span>
            {a.origin === "controller" && (
              <span className="mcp-approvals__client t-meta">controller</span>
            )}
            <span className="mcp-approvals__client t-meta">
              {a.input_summary}
            </span>
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
              Deny
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
