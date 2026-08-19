import { useEffect, useState } from "react";

import { Block } from "../../components/common/Block";
import { Button } from "../../components/common/Button";
import { Checkbox } from "../../components/common/Checkbox";
import { Input } from "../../components/common/Input";
import { Note } from "../../components/common/Note";
import { Radio } from "../../components/common/Radio";
import { Range } from "../../components/common/Range";
import { Hint } from "../../components/ui/Hint";
import { ResetDefaults } from "../../components/common/ResetDefaults";
import {
  fetchSessionPolicy,
  postCompactThreshold,
  postResetDefaults,
  postSessionPolicy,
  type SessionPolicyResponse,
  type SessionResumePolicy,
} from "../../lib/api";
import type { IdentityCompactThreshold } from "../../lib/types";
import { useCachedFetch } from "../../lib/useCachedFetch";
import { useIdentityStore } from "../../stores/identity";
import { useSessionPolicyStore } from "../../stores/sessionPolicy";

interface PolicyOption {
  value: SessionResumePolicy;
  label: string;
  hint: string;
}

const POLICY_OPTIONS: PolicyOption[] = [
  {
    value: "today_only",
    label: "today only",
    hint: "Only sessions started today auto-resume on reload.",
  },
  {
    value: "today_plus_yesterday",
    label: "today + yesterday",
    hint: "The default. Covers an overnight gap.",
  },
  {
    value: "n_days",
    label: "last N days",
    hint: "Resume any session newer than the slider days.",
  },
  {
    value: "always",
    label: "always",
    hint: "Auto-resume any saved session, however old.",
  },
];

/** Everything that governs a session's life: when it is written to disk, how
 *  long it stays resumable, and when its history gets compacted.
 *
 *  One section rather than three, because the operator's question is "what
 *  happens to my conversation" — and the answer used to be split between a
 *  panel called Compaction and a sibling called Session policy.
 */
export function SessionControlSection() {
  const setPolicyStore = useSessionPolicyStore((s) => s.set);
  const {
    data: server,
    error,
    setError,
    set: setServer,
  } = useCachedFetch<SessionPolicyResponse>(
    "settings.session-policy",
    fetchSessionPolicy,
  );
  const [policy, setPolicy] = useState<SessionResumePolicy>(
    "today_plus_yesterday",
  );
  const [days, setDays] = useState(1);
  const [autosave, setAutosave] = useState(true);
  const [interval, setIntervalSeconds] = useState("60");
  const [showToasts, setShowToasts] = useState(true);
  const [saving, setSaving] = useState(false);

  // The form fields mirror the server value, so they re-seed whenever a fetch
  // lands — first paint, a cached revisit, or a revalidate after a reconnect.
  useEffect(() => {
    if (!server) return;
    setPolicy(server.policy);
    setDays(server.days);
    // `??` rather than a bare read: the backend updates from production while
    // the SPA is compiled into the installer, so a newer exe can talk to an
    // older backend that has never heard of these fields. Undefined would make
    // the checkbox uncontrolled.
    setAutosave(server.autosave ?? true);
    setIntervalSeconds(String(server.autosave_interval_seconds ?? 60));
    setShowToasts(server.show_config_reload_toasts);
    setPolicyStore({
      policy: server.policy,
      days: server.days,
      show_config_reload_toasts: server.show_config_reload_toasts,
    });
  }, [server, setPolicyStore]);

  const intervalNum = parseInt(interval, 10);
  const intervalValid =
    Number.isFinite(intervalNum) && intervalNum >= 10 && intervalNum <= 3600;

  const dirty =
    server !== null &&
    (policy !== server.policy ||
      days !== server.days ||
      autosave !== server.autosave ||
      (intervalValid && intervalNum !== server.autosave_interval_seconds) ||
      showToasts !== server.show_config_reload_toasts);

  const save = async () => {
    if (!intervalValid) return;
    setSaving(true);
    setError(null);
    try {
      await postSessionPolicy({
        policy,
        days,
        autosave,
        autosave_interval_seconds: intervalNum,
        show_config_reload_toasts: showToasts,
      });
      const fresh = await fetchSessionPolicy();
      setServer(fresh);
      setPolicyStore({
        policy: fresh.policy,
        days: fresh.days,
        show_config_reload_toasts: fresh.show_config_reload_toasts,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="settings-section">
      {error && <Note tone="bad">{error}</Note>}

      <Block
        title="Autosave"
        titleHint="Writes the open session to disk on a timer while it is running. Without it a session is only saved when the app shuts down cleanly, so a crash or a forced kill loses every turn it ran."
      >
        <div className="session-policy-toggle">
          <Checkbox
            checked={autosave}
            onChange={setAutosave}
            label="save the open session while it runs"
          />
        </div>
        <div className="session-policy-days">
          <label className="t-meta">every</label>
          <Input
            type="number"
            min={10}
            max={3600}
            step={10}
            value={interval}
            onChange={setIntervalSeconds}
            disabled={!autosave}
            className="cost-row__input"
            ariaLabel="autosave interval in seconds"
          />
          <span className="t-meta">seconds</span>
        </div>
        {!intervalValid && (
          <Note tone="warn">Interval must be between 10 and 3600 seconds.</Note>
        )}
      </Block>

      <Block
        title="Resume"
        titleHint="How old a saved session may be and still reopen by itself when Mirror reloads. Older sessions stay in the Sessions drawer for a manual /load."
      >
        <div className="session-policy-options">
          {POLICY_OPTIONS.map((opt) => (
            <Radio
              key={opt.value}
              name="resume_policy"
              checked={policy === opt.value}
              onChange={() => setPolicy(opt.value)}
              label={opt.label}
              hint={opt.hint}
            />
          ))}
        </div>
        {policy === "n_days" && (
          <div className="session-policy-days">
            <label className="t-meta">days</label>
            <Range
              min={1}
              max={365}
              step={1}
              value={days}
              onChange={setDays}
              ariaLabel="resume window in days"
              className="session-policy-days__range"
            />
            <span className="session-policy-days-value">{days}</span>
          </div>
        )}
        <div className="session-policy-toggle">
          <Checkbox
            checked={showToasts}
            onChange={setShowToasts}
            label="show config-reload toasts"
          />
          <span className="t-meta">
            fires when external edits to tesseract/config/*.yaml reflect live
          </span>
        </div>
      </Block>

      <div className="session-policy-actions">
        <Button onClick={save} disabled={!dirty || saving || !intervalValid}>
          {saving ? "saving…" : "save"}
        </Button>
        <ResetDefaults
          run={() => postResetDefaults("session")}
          reach="autosave, resume and compaction"
          // Both halves of this pane: the session block is served by
          // `/api/settings/session-policy`, the compaction knobs below ride
          // on the identity payload.
          onDone={() => {
            void fetchSessionPolicy().then(setServer);
            void useIdentityStore.getState().fetchIdentity();
          }}
        />
      </div>

      <CompactionBlock />
    </section>
  );
}

/** Compaction commits per control rather than behind the save button above —
 *  it writes roles.yaml, not the session block, and kept its own contract when
 *  the two panels merged. */
function CompactionBlock() {
  const thresholds = useIdentityStore((s) => s.compactThresholds);
  const setCompactThreshold = useIdentityStore((s) => s.setCompactThreshold);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const chat = thresholds?.chat_brain ?? null;
  const [draftRatio, setDraftRatio] = useState<number>(chat?.ratio ?? 0.4);
  const [keepDraft, setKeepDraft] = useState<string>(
    chat?.keep_recent_turns != null ? String(chat.keep_recent_turns) : "10",
  );

  useEffect(() => {
    if (chat) setDraftRatio(chat.ratio);
  }, [chat]);

  useEffect(() => {
    if (chat) setKeepDraft(String(chat.keep_recent_turns ?? 10));
  }, [chat]);

  const commitRatio = async () => {
    if (!chat || draftRatio === chat.ratio) return;
    setSaving(true);
    setError(null);
    try {
      const res = await postCompactThreshold({ role: "chat_brain", ratio: draftRatio });
      setCompactThreshold("chat_brain", res as IdentityCompactThreshold);
    } catch (err) {
      setError(err instanceof Error ? err.message : "compact-threshold update failed");
      setDraftRatio(chat.ratio);
    } finally {
      setSaving(false);
    }
  };

  const commitKeep = async () => {
    if (!chat) return;
    const next = parseInt(keepDraft, 10);
    if (!Number.isFinite(next) || next === chat.keep_recent_turns) {
      setKeepDraft(String(chat.keep_recent_turns));
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const res = await postCompactThreshold({
        role: "chat_brain",
        keep_recent_turns: next,
      });
      setCompactThreshold("chat_brain", res as IdentityCompactThreshold);
    } catch (err) {
      setError(err instanceof Error ? err.message : "keep_recent_turns update failed");
      setKeepDraft(String(chat.keep_recent_turns));
    } finally {
      setSaving(false);
    }
  };

  const draftTokens = chat ? Math.round(draftRatio * chat.context_window) : 0;

  return (
    <Block
      title="Compaction"
      titleHint="How full the context window gets before older history is summarised away. Applies to the primary chat_brain entry; fallback models keep their own tuning."
    >
      <div className="compact-row">
        <span className="compact-row__role">chat_brain</span>
        <Range
          min={0.1}
          max={0.95}
          step={0.01}
          value={draftRatio}
          onChange={setDraftRatio}
          onCommit={commitRatio}
          disabled={!chat || saving}
          ariaLabel="chat_brain compact threshold"
        />
        <span className="compact-row__ratio">{(draftRatio * 100).toFixed(0)}%</span>
        <span className="compact-row__tokens t-meta">
          → {draftTokens.toLocaleString()} tok
        </span>
      </div>
      <div className="compact-row compact-row--keep">
        <span className="compact-row__role">keep_recent_turns</span>
        <Hint
          label="How many recent messages survive untouched when history is compacted."
          maxWidth={360}
        >
          <Input
            type="number"
            min={2}
            max={200}
            step={1}
            value={keepDraft}
            onChange={setKeepDraft}
            onBlur={commitKeep}
            onKeyDown={(e) => {
              if (e.key === "Enter") (e.target as HTMLInputElement).blur();
            }}
            disabled={!chat || saving}
            className="cost-row__input"
            ariaLabel="chat_brain keep_recent_turns"
          />
        </Hint>
        <span className="t-meta">turns kept verbatim</span>
      </div>
      <div className="compact-row compact-row--disabled">
        <span className="compact-row__role">observer_agent</span>
        <span className="t-meta">Fixed reset on arm/disarm — no compaction.</span>
      </div>
      <Note>
        For deeper knobs, edit `tesseract/config/roles.yaml` directly.
      </Note>
      {error && <Note tone="bad">{error}</Note>}
    </Block>
  );
}
