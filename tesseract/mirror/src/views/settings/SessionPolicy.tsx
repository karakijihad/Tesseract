import { useEffect, useState } from "react";

import {
  fetchSessionPolicy,
  postSessionPolicy,
  type SessionPolicyResponse,
  type SessionResumePolicy,
} from "../../lib/api";
import { useSessionPolicyStore } from "../../stores/sessionPolicy";
import { useWebSocketStore } from "../../stores/websocket";
import { useFetchRetryTick } from "../../lib/useFetchRetry";

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
    hint: "Phase 15 default. Covers an overnight gap; matches DAILY_FILES_TO_LOAD=2.",
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

export function SessionPolicySection() {
  const setPolicyStore = useSessionPolicyStore((s) => s.set);
  const [server, setServer] = useState<SessionPolicyResponse | null>(null);
  const [policy, setPolicy] = useState<SessionResumePolicy>(
    "today_plus_yesterday",
  );
  const [days, setDays] = useState(1);
  const [showToasts, setShowToasts] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Re-runs on every WS (re)connection: a backend restart must replace a
  // pre-restart "Failed to fetch" with fresh data (2026-07-30).
  const wsGeneration = useWebSocketStore((s) => s.generation);
  const retryTick = useFetchRetryTick(error !== null);
  useEffect(() => {
    setError(null);
    fetchSessionPolicy()
      .then((res) => {
        setServer(res);
        setPolicy(res.policy);
        setDays(res.days);
        setShowToasts(res.show_config_reload_toasts);
        setPolicyStore({
          policy: res.policy,
          days: res.days,
          show_config_reload_toasts: res.show_config_reload_toasts,
        });
      })
      .catch((err) =>
        setError(err instanceof Error ? err.message : String(err)),
      );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [setPolicyStore, wsGeneration, retryTick]);

  const dirty =
    server !== null &&
    (policy !== server.policy ||
      days !== server.days ||
      showToasts !== server.show_config_reload_toasts);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await postSessionPolicy({
        policy,
        days,
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
      <h3 className="settings-section__title">Session resume</h3>
      <div className="settings-hint t-meta">
        Auto-resume cutoff for the persisted save name on Mirror reload. Older
        sessions still appear in the Sessions drawer for manual /load.
      </div>
      {error && <div className="settings-error">{error}</div>}
      <div className="session-policy-options">
        {POLICY_OPTIONS.map((opt) => (
          <label key={opt.value} className="session-policy-option">
            <input
              type="radio"
              name="resume_policy"
              checked={policy === opt.value}
              onChange={() => setPolicy(opt.value)}
            />
            <span className="session-policy-label">{opt.label}</span>
            <span className="session-policy-hint t-meta">{opt.hint}</span>
          </label>
        ))}
      </div>
      {policy === "n_days" && (
        <div className="session-policy-days">
          <label className="t-meta">days</label>
          <input
            type="range"
            min={1}
            max={365}
            step={1}
            value={days}
            onChange={(e) => setDays(parseInt(e.target.value, 10))}
          />
          <span className="session-policy-days-value">{days}</span>
        </div>
      )}
      <div className="session-policy-toggle">
        <label className="session-policy-checkbox">
          <input
            type="checkbox"
            checked={showToasts}
            onChange={(e) => setShowToasts(e.target.checked)}
          />
          show config-reload toasts
        </label>
        <span className="t-meta">
          fires when external edits to tesseract/config/*.yaml reflect live
        </span>
      </div>
      <div className="session-policy-actions">
        <button
          type="button"
          className="session-policy-save"
          onClick={save}
          disabled={!dirty || saving}
        >
          {saving ? "saving…" : "save"}
        </button>
      </div>
    </section>
  );
}
