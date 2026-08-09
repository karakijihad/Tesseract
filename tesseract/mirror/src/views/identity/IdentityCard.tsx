import { useEffect, useState } from "react";

import { useIdentityStore } from "../../stores/identity";
import { useWebSocketStore } from "../../stores/websocket";
import { useFetchRetryTick } from "../../lib/useFetchRetry";
import { fetchIdentity, fetchVoiceSettings, saveIdentity } from "../../lib/api";
import type { IdentitySavePatch } from "../../lib/types";

/** The names, and the wake phrase built from one of them.
 *
 * Writes go to `POST /api/identity`, the only sanctioned writer for
 * `mirror.yaml::identity` — `file_write` keeps that file locked, so a tool
 * cannot rename the agent. The route broadcasts `identity_changed`, which
 * lands in the identity store, so every rendered surface follows without a
 * reload (and a second cockpit window follows too).
 *
 * The wake block lives here rather than in Settings → Voice because the
 * phrase is `<prefix> <name>` — the name it is built from is the field
 * directly above it.
 */
export function IdentityCard() {
  const storeName = useIdentityStore((s) => s.name);
  const storeOperator = useIdentityStore((s) => s.operatorName);

  const [name, setName] = useState(storeName);
  const [operator, setOperator] = useState(storeOperator);
  // Read-only facts from the same `/api/identity` response the names come
  // from, rather than from the store — the store is filled on WS connect,
  // and reading it here rendered an empty bordered box until that landed.
  const [facts, setFacts] = useState<
    { version: string; model: string; provider: string; mode: string } | null
  >(null);
  const [wakeEnabled, setWakeEnabled] = useState(false);
  const [wakePrefix, setWakePrefix] = useState("");
  const [wakeThreshold, setWakeThreshold] = useState<number | null>(null);
  const [saved, setSaved] = useState<{
    name: string;
    operator: string;
    wakeEnabled: boolean;
    wakePrefix: string;
  } | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The broadcast landing (this window's own save, or another window's)
  // re-seeds the inputs. An empty store value means "not loaded yet", not
  // "renamed to nothing" — seeding from it would blank the field the
  // parallel read below is about to fill.
  useEffect(() => {
    if (!storeName) return;
    setName(storeName);
    setSaved((prev) => (prev ? { ...prev, name: storeName } : prev));
  }, [storeName]);
  useEffect(() => {
    if (!storeOperator) return;
    setOperator(storeOperator);
    setSaved((prev) => (prev ? { ...prev, operator: storeOperator } : prev));
  }, [storeOperator]);

  // Two reads, in parallel — neither depends on the other.
  //
  // The names come from `GET /api/identity` rather than from whatever the
  // store happens to hold: the store is filled on WS connect, and a card
  // that waited for that would show the operator an empty name field
  // whenever the socket was slow or down.
  //
  // Wake state is not on that endpoint — it comes from the voice settings
  // read, which serves it from mirror.yaml rather than the live config so
  // a just-saved value isn't flipped back by the watcher's debounce.
  //
  // Re-runs on every WS (re)connection so a backend restart replaces a
  // stale "Failed to fetch" with fresh data.
  const wsGeneration = useWebSocketStore((s) => s.generation);
  const retryTick = useFetchRetryTick(error !== null);
  useEffect(() => {
    setError(null);
    Promise.all([fetchIdentity(), fetchVoiceSettings()])
      .then(([ident, voice]) => {
        setName(ident.name);
        setOperator(ident.operator_name);
        setFacts({
          version: ident.version,
          model: ident.model_name,
          provider: ident.provider,
          mode: ident.security_mode,
        });
        setWakeEnabled(voice.wake_word_enabled);
        setWakePrefix(voice.wake_word_prefix);
        setWakeThreshold(voice.wake_word_threshold);
        setSaved({
          name: ident.name,
          operator: ident.operator_name,
          wakeEnabled: voice.wake_word_enabled,
          wakePrefix: voice.wake_word_prefix,
        });
      })
      .catch((err) =>
        setError(err instanceof Error ? err.message : String(err)),
      );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wsGeneration, retryTick]);

  const trimmedName = name.trim();
  const trimmedOperator = operator.trim();
  const trimmedPrefix = wakePrefix.trim();
  const dirty =
    saved !== null &&
    (trimmedName !== saved.name ||
      trimmedOperator !== saved.operator ||
      wakeEnabled !== saved.wakeEnabled ||
      trimmedPrefix !== saved.wakePrefix);
  // The backend refuses a blank name or prefix; disabling save says so
  // before the round trip instead of surfacing a 400 for a typo.
  const valid = trimmedName !== "" && trimmedOperator !== "" && trimmedPrefix !== "";

  const save = async () => {
    if (!saved || !dirty || !valid) return;
    const patch: IdentitySavePatch = {};
    if (trimmedName !== saved.name) patch.name = trimmedName;
    if (trimmedOperator !== saved.operator) patch.operator_name = trimmedOperator;
    const wake: Partial<{ enabled: boolean; prefix: string }> = {};
    if (wakeEnabled !== saved.wakeEnabled) wake.enabled = wakeEnabled;
    if (trimmedPrefix !== saved.wakePrefix) wake.prefix = trimmedPrefix;
    if (Object.keys(wake).length > 0) patch.wake_word = wake;

    setSaving(true);
    setError(null);
    try {
      const applied = await saveIdentity(patch);
      setSaved({
        name: applied.name,
        operator: applied.operator_name,
        wakeEnabled: applied.wake_word.enabled,
        wakePrefix: applied.wake_word.prefix,
      });
      setWakeEnabled(applied.wake_word.enabled);
      setWakePrefix(applied.wake_word.prefix);
      // The `identity_changed` broadcast re-seeds the name inputs and every
      // other surface; this only closes the loop for the saving window if
      // its own socket happens to be down.
      useIdentityStore.getState().setNames(applied.name, applied.operator_name);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const phrase = `${trimmedPrefix} ${trimmedName}`.trim();

  return (
    <section className="identity-view-card identity-panel">
      <div className="identity-view-card-heading t-meta">Identity</div>
      <div className="identity-panel-body">
        {error && <div className="settings-error">{error}</div>}

        <div className="identity-field">
          <label className="identity-field-label t-meta" htmlFor="identity-name">
            name
          </label>
          <input
            id="identity-name"
            className="identity-input"
            value={name}
            maxLength={40}
            disabled={saved === null}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void save()}
          />
          <span className="t-meta identity-field-hint">
            What it answers to, everywhere. Renaming leaves the workspace
            documents alone — those are prose it wrote, not a template.
          </span>
        </div>

        <div className="identity-field">
          <label className="identity-field-label t-meta" htmlFor="identity-operator">
            operator
          </label>
          <input
            id="identity-operator"
            className="identity-input"
            value={operator}
            maxLength={40}
            disabled={saved === null}
            onChange={(e) => setOperator(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void save()}
          />
          <span className="t-meta identity-field-hint">
            Your display name in chat bubbles and greetings.
          </span>
        </div>

        <div className="identity-field">
          <label className="identity-field-label t-meta" htmlFor="identity-wake">
            wake word
          </label>
          <div className="identity-wake-row">
            <input
              id="identity-wake"
              type="checkbox"
              checked={wakeEnabled}
              disabled={saved === null}
              onChange={(e) => setWakeEnabled(e.target.checked)}
            />
            <input
              className="identity-input identity-input--short"
              value={wakePrefix}
              maxLength={40}
              disabled={saved === null}
              aria-label="wake word prefix"
              onChange={(e) => setWakePrefix(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void save()}
            />
            <span className="t-meta">
              {wakeEnabled
                ? `Command and Speak act only on speech starting with “${phrase}”.`
                : `Off — every utterance dispatches. On, only “${phrase}” does.`}
            </span>
          </div>
          <span className="t-meta identity-field-hint">
            Transcribe and Terminal are never gated. Match tolerance is{" "}
            <code>identity.wake_word.match_threshold</code> in{" "}
            <code>mirror.yaml</code>
            {wakeThreshold !== null && <> (currently {wakeThreshold})</>}.
          </span>
        </div>

        {facts && (
          <dl className="soul-identity-grid t-caption identity-facts">
            <dt>version</dt>
            <dd>{facts.version}</dd>
            <dt>model</dt>
            <dd>
              {facts.model}
              {facts.provider && (
                <span className="t-meta soul-identity-provider">{` · ${facts.provider}`}</span>
              )}
            </dd>
            <dt>mode</dt>
            <dd>{facts.mode}</dd>
          </dl>
        )}

        <div className="identity-actions">
          <button
            type="button"
            className="identity-save"
            onClick={() => void save()}
            disabled={!dirty || !valid || saving}
          >
            {saving ? "saving…" : "save"}
          </button>
          {dirty && !valid && (
            <span className="t-meta">Name, operator and prefix cannot be blank.</span>
          )}
        </div>
      </div>
    </section>
  );
}
