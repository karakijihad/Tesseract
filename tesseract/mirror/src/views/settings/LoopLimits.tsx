import { useEffect, useState } from 'react';

import { Hint } from '../../components/ui/Hint';
import {
  fetchSessionCaps,
  postSessionCaps,
  type SessionCapsResponse,
} from '../../lib/api';

export function LoopLimitsSection() {
  const [server, setServer] = useState<SessionCapsResponse | null>(null);
  const [toolCap, setToolCap] = useState<string>('25');
  const [errCap, setErrCap] = useState<string>('3');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchSessionCaps()
      .then((res) => {
        setServer(res);
        setToolCap(String(res.tool_iteration_cap));
        setErrCap(String(res.consecutive_error_cap));
      })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, []);

  const commitToolCap = async () => {
    if (!server) return;
    const next = parseInt(toolCap, 10);
    if (!Number.isFinite(next) || next === server.tool_iteration_cap) {
      setToolCap(String(server.tool_iteration_cap));
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const res = await postSessionCaps({ tool_iteration_cap: next });
      setServer(res);
      setToolCap(String(res.tool_iteration_cap));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'tool_iteration_cap update failed');
      setToolCap(String(server.tool_iteration_cap));
    } finally {
      setSaving(false);
    }
  };

  const commitErrCap = async () => {
    if (!server) return;
    const next = parseInt(errCap, 10);
    if (!Number.isFinite(next) || next === server.consecutive_error_cap) {
      setErrCap(String(server.consecutive_error_cap));
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const res = await postSessionCaps({ consecutive_error_cap: next });
      setServer(res);
      setErrCap(String(res.consecutive_error_cap));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'consecutive_error_cap update failed');
      setErrCap(String(server.consecutive_error_cap));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="settings-section">
      <h3 className="settings-section__title">Loop limits</h3>
      <div className="compact-row compact-row--keep">
        <span className="compact-row__role">tool_iteration_cap</span>
        <Hint
          label="Hard cap on tool-call iterations per turn. Prevents runaway tool loops."
          maxWidth={360}
        >
          <input
            type="number"
            min={1}
            max={200}
            step={1}
            value={toolCap}
            onChange={(e) => setToolCap(e.target.value)}
            onBlur={commitToolCap}
            onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
            disabled={!server || saving}
            className="cost-row__input"
            aria-label="tool_iteration_cap"
          />
        </Hint>
        <span className="t-meta">tool calls per turn</span>
      </div>
      <div className="compact-row compact-row--keep">
        <span className="compact-row__role">consecutive_error_cap</span>
        <Hint
          label="Adapter ERRORs in a row before chat_brain's circuit-breaker trips and surfaces a final ERROR. Resets on any successful response."
          maxWidth={360}
        >
          <input
            type="number"
            min={1}
            max={20}
            step={1}
            value={errCap}
            onChange={(e) => setErrCap(e.target.value)}
            onBlur={commitErrCap}
            onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
            disabled={!server || saving}
            className="cost-row__input"
            aria-label="consecutive_error_cap"
          />
        </Hint>
        <span className="t-meta">errors before breaker trips</span>
      </div>
      <div className="compact-row compact-row--disabled">
        <span className="compact-row__role">DENY rules</span>
        <span className="t-meta">
          Locked. The 24-check bash_security DENY list is non-configurable per the
          security contract.
        </span>
      </div>
      <div className="settings-hint t-meta">
        Edits to <code>roles.chat_brain.tool_iteration_cap</code> /{' '}
        <code>consecutive_error_cap</code> in <code>tesseract/config/roles.yaml</code> reflect
        live; this panel mirrors the file. Live ChatSessions pick up new caps on the next turn.
      </div>
      {error && <div className="settings-error">{error}</div>}
    </section>
  );
}
