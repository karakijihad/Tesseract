import { useEffect, useState } from "react";

import { Block } from "../../components/common/Block";
import { Button } from "../../components/common/Button";
import { Checkbox } from "../../components/common/Checkbox";
import { Input } from "../../components/common/Input";
import { Note } from "../../components/common/Note";
import { Tabs, type TabItem } from "../../components/common/Tabs";
import { CompactionBar } from "./CompactionBar";
import { LoopLimitsSection } from "./LoopLimits";
import { Radio } from "../../components/common/Radio";
import { Range } from "../../components/common/Range";
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
import { useConversationStore } from "../../stores/conversation";
import { useSessionStore } from "../../stores/session";
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
type Pane = "autosave" | "compaction" | "loop-limits";

/** Three questions about one conversation: what is written down while it
 *  runs, what happens to it when it gets long, and where it is made to stop.
 *  One scroll held all three and the answer to any one of them was somewhere
 *  in it. */
const PANES: readonly TabItem<Pane>[] = [
  { key: "autosave", label: "Autosave" },
  { key: "compaction", label: "Compaction" },
  { key: "loop-limits", label: "Loop limits" },
];

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
  const [pane, setPane] = useState<Pane>("autosave");

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

  if (pane !== "autosave") {
    return (
      <section className="settings-section">
        <Tabs items={PANES} active={pane} onSelect={setPane} label="Chat panes" />
        {pane === "compaction" ? <CompactionBlock /> : <LoopLimitsSection />}
      </section>
    );
  }

  return (
    <section className="settings-section">
      <Tabs items={PANES} active={pane} onSelect={setPane} label="Chat panes" />
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
        titleHint="How old a saved session may be and still reopen by itself when Mirror reloads. Older sessions stay in the conversations rail, where you can open them again."
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
          reach="autosave and resume"
          onDone={() => void fetchSessionPolicy().then(setServer)}
        />
      </div>
    </section>
  );
}

/** Compaction commits per control rather than behind the save button above —
 *  it writes roles.yaml, not the session block, and kept its own contract when
 *  the two panels merged. */
function CompactionBlock() {
  const thresholds = useIdentityStore((s) => s.compactThresholds);
  const setCompactThreshold = useIdentityStore((s) => s.setCompactThreshold);
  // The measured floor, and the conversation it belongs to. A chat switch
  // leaves the previous chat's numbers standing until the new one runs a turn,
  // so they are only used while they still describe what is on screen.
  const storedStats = useSessionStore((s) => s.latestStats);
  const statsChatId = useSessionStore((s) => s.latestStatsChatId);
  const activeChatId = useConversationStore((s) => s.activeChatId);
  const stats =
    statsChatId == null || statsChatId === activeChatId ? storedStats : null;
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const chat = thresholds?.chat_brain ?? null;
  // No second copy of the shipped defaults: 0.4 and 10 were both wrong after
  // the config moved to 0.25 and 5, and nobody noticed because the controls
  // are disabled until `chat` arrives. Zero and an empty field are visibly
  // "not loaded yet" rather than plausibly wrong.
  const [draftRatio, setDraftRatio] = useState<number>(chat?.ratio ?? 0);
  const [keepDraft, setKeepDraft] = useState<string>(
    chat?.keep_recent_turns != null ? String(chat.keep_recent_turns) : "",
  );

  useEffect(() => {
    if (chat) setDraftRatio(chat.ratio);
  }, [chat]);

  useEffect(() => {
    if (chat?.keep_recent_turns != null) setKeepDraft(String(chat.keep_recent_turns));
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
    // `parseInt` on "2.9" is 2, so the field used to swallow a fraction and
    // save a number the operator never typed. The route refuses one; let it.
    const next = Number(keepDraft);
    if (
      !Number.isFinite(next) ||
      !Number.isInteger(next) ||
      next === chat.keep_recent_turns
    ) {
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

  return (
    // The tab strip above already says Compaction, so the block said it again
    // six pixels below while the sentence explaining it stayed hidden behind
    // an ⓘ. The sentence is the half worth showing.
    <Block title={null}>
      <Note>
        How full the context window gets before older history is summarised
        away, and how much of the recent conversation survives that word for
        word. A fallback model keeps its own tuning.
      </Note>
      <CompactionBar
        facts={{
          window: chat?.context_window ?? 0,
          headroom: chat?.headroom_multiplier ?? null,
          comfortable: chat?.comfortable_multiplier ?? null,
          // The SHIPPED value, not the running one. `compact_ratio` is what
          // this pane writes, so a line drawn from it sits on the handle and
          // marks nothing.
          defaultRatio: chat?.shipped_ratio ?? null,
          // The measured half comes from the stats envelope alone. It is the
          // only copy that belongs to a particular conversation: `/api/identity`
          // answers a GET with no session and no chat, so anything measured
          // there is some open chat's, not necessarily this one's.
          anchorTokens: stats?.head_anchor_tokens ?? null,
          tailTokens: stats?.tail_tokens ?? null,
          tailTurns: stats?.tail_turns ?? null,
          systemTokens: stats?.system_tokens ?? null,
          ratioFloor: chat?.ratio_min ?? null,
          ratioCeiling: chat?.ratio_max ?? null,
        }}
        ratio={draftRatio}
        turns={keepDraft}
        turnsMin={chat?.turns_min ?? 0}
        turnsMax={chat?.turns_max ?? 0}
        disabled={!chat || saving}
        onRatio={setDraftRatio}
        onRatioCommit={commitRatio}
        onTurns={setKeepDraft}
        onTurnsCommit={commitKeep}
      />
      <div className="compact-row compact-row--disabled">
        <span className="compact-row__role">observer_agent</span>
        <span className="t-meta">Reset when it is switched on or off. Never compacted.</span>
      </div>
      <div className="compact-bar__actions">
        <ResetDefaults
          run={() => postResetDefaults("compaction")}
          reach="the threshold and the turns kept"
          onDone={() => void useIdentityStore.getState().fetchIdentity()}
        />
        <span className="t-meta">
          For deeper knobs, edit tesseract/config/roles.yaml directly.
        </span>
      </div>
      {error && <Note tone="bad">{error}</Note>}
    </Block>
  );
}
