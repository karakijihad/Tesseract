import { Note } from "../../components/common/Note";
import { useCachedFetch } from "../../lib/useCachedFetch";
import { useState } from "react";

import { Hint } from "../../components/ui/Hint";
import { Block } from "../../components/common/Block";
import { Input } from "../../components/common/Input";
import { ResetDefaults } from "../../components/common/ResetDefaults";
import {
  fetchSessionCaps,
  postResetDefaults,
  postSessionCaps,
  type DenyRule,
  type SessionCapsResponse,
} from "../../lib/api";

/** The bash DENY/ASK floor, as the runtime holds it.
 *
 * This row said "Locked. The 24-check bash_security DENY list" and the module
 * had 26 checks — a hand-written count beside a list that grows. It is read
 * from the code that owns the rules now, and it renders them rather than
 * asserting they exist, because "you cannot change this" is a claim an
 * operator is entitled to see the substance of.
 */
function DenyRules({
  rules,
  locked,
}: {
  rules: DenyRule[] | undefined;
  locked: boolean | undefined;
}) {
  if (!rules) {
    return (
      <Block title="Command DENY rules">
        <Note tone="warn">
          This backend did not report its rule list. It is still enforcing one —
          the checks run in the runtime, not here — but nothing on this screen
          can show you which, so treat the list as unknown rather than empty.
        </Note>
      </Block>
    );
  }
  const blocked = rules.filter((r) => r.posture === "blocked");
  const ask = rules.filter((r) => r.posture === "ask");
  const mixed = rules.filter((r) => r.posture === "mixed");
  return (
    <Block
      title="Command DENY rules"
      titleHint={
        "Every bash command the assistant runs is checked against these before any " +
        "permission in permissions.yaml is consulted. They are the floor the rest of " +
        "the permission system sits on."
      }
      meta={
        `${rules.length} checks · ${blocked.length} refuse · ${ask.length} ask` +
        (mixed.length > 0 ? ` · ${mixed.length} both` : "")
      }
    >
      <Note tone="warn">
        <strong>You cannot change these, and neither can the assistant.</strong>{" "}
        There is no setting here, in <code>permissions.yaml</code>, or in any
        hook, plugin, skill or agent that relaxes them — a mode that switches
        every tool to AUTO does not reach them either.
        {locked === false && " This backend reports them as unlocked, which it should not — that is a defect worth reporting."}
      </Note>
      <Note>
        Editing <code>tesseract/permissions/bash_security.py</code> by hand is
        the only way to move one, and what happens if you do is exactly what it
        sounds like: the check stops running, for every caller, with nothing
        else standing behind it. The audit log records a refusal by NUMBER, so
        the numbers below are what you will see there. An update replaces that
        file, so a hand-edit is also silently reverted on the next one.
      </Note>
      <div className="deny-rules">
        {rules.map((r) => (
          <div key={r.check} className="deny-rules__row">
            <span className="deny-rules__num t-meta">
              {String(r.check).padStart(2, "0")}
            </span>
            <span
              className={`deny-rules__posture deny-rules__posture--${r.posture}`}
            >
              {r.posture === "blocked"
                ? "REFUSED"
                : r.posture === "mixed"
                  ? "BOTH"
                  : "ASKS YOU"}
            </span>
            <span className="deny-rules__what">{r.refuses}</span>
          </div>
        ))}
      </div>
      <Note>
        <strong>ASKS YOU</strong> means the command stops and waits for you —
        it is never auto-allowed, and with no operator attached it fails
        closed. <strong>REFUSED</strong> does not ask at all; there is no
        answer that lets it through.
        {mixed.length > 0 && (
          <>
            {" "}
            <strong>BOTH</strong> is a check with more than one pattern, where
            some ask and some refuse — read the description for which is which,
            and assume the refusing half applies to you.
          </>
        )}
      </Note>
    </Block>
  );
}

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

      <DenyRules rules={server?.deny_rules} locked={server?.deny_rules_locked} />
    </section>
  );
}
