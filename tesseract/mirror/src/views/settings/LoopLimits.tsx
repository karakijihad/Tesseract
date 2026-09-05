import { Note } from "../../components/common/Note";
import { useCachedFetch } from "../../lib/useCachedFetch";
import { useState } from "react";

import { Hint } from "../../components/ui/Hint";
import { Input } from "../../components/common/Input";
import { ResetDefaults } from "../../components/common/ResetDefaults";
import {
  fetchSessionCaps,
  postResetDefaults,
  postSessionCaps,
  type SessionCapsResponse,
} from "../../lib/api";

export function LoopLimitsSection() {
  const {
    data: server,
    error,
    setError,
    set: setServer,
  } = useCachedFetch<SessionCapsResponse>(
    "settings.session-caps",
    fetchSessionCaps,
  );
  const [toolCap, setToolCap] = useState<string>("25");
  const [errCap, setErrCap] = useState<string>("3");
  const [saving, setSaving] = useState(false);


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
      setError(
        err instanceof Error ? err.message : "tool_iteration_cap update failed",
      );
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
      setError(
        err instanceof Error
          ? err.message
          : "consecutive_error_cap update failed",
      );
      setErrCap(String(server.consecutive_error_cap));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="settings-section">
      <div className="compact-row compact-row--keep">
        <span className="compact-row__role">tool_iteration_cap</span>
        <Hint
          label="Hard cap on tool-call iterations per turn. Prevents runaway tool loops."
          maxWidth={360}
        >
          <Input
            type="number"
            min={1}
            max={200}
            step={1}
            value={toolCap}
            onChange={setToolCap}
            onBlur={commitToolCap}
            onKeyDown={(e) => {
              if (e.key === "Enter") (e.target as HTMLInputElement).blur();
            }}
            disabled={!server || saving}
            className="cost-row__input"
            ariaLabel="tool_iteration_cap"
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
          <Input
            type="number"
            min={1}
            max={20}
            step={1}
            value={errCap}
            onChange={setErrCap}
            onBlur={commitErrCap}
            onKeyDown={(e) => {
              if (e.key === "Enter") (e.target as HTMLInputElement).blur();
            }}
            disabled={!server || saving}
            className="cost-row__input"
            ariaLabel="consecutive_error_cap"
          />
        </Hint>
        <span className="t-meta">errors before breaker trips</span>
      </div>
      <div className="session-policy-actions">
        <ResetDefaults
          run={() => postResetDefaults("loop_limits")}
          reach="both caps above"
          onDone={() => void fetchSessionCaps().then(setServer)}
        />
      </div>
      <Note>
        Edits to <code>roles.chat_brain.tool_iteration_cap</code> /{" "}
        <code>consecutive_error_cap</code> in{" "}
        <code>tesseract/config/roles.yaml</code> reflect live; this panel
        mirrors the file. Live ChatSessions pick up new caps on the next turn.
      </Note>
      {error && <Note tone="bad">{error}</Note>}
    </section>
  );
}
