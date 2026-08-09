import { useEffect, useState } from "react";

import { usePanelStore } from "../../cockpit/panelStore";
import { useWebSocketStore } from "../../stores/websocket";
import { useFetchRetryTick } from "../../lib/useFetchRetry";
import { fetchVoiceSettings, type VoiceSettingsResponse } from "../../lib/api";

/** What speech sounds like, read-only.
 *
 * The two controls that used to live here moved to the Identity tab in
 * AS-5: the wake phrase (it is `<prefix> <name>`, so it belongs beside
 * the name) and the voice itself. What remains is the character each
 * surface renders — per-provider `synthesis_presets`, which are a config
 * edit the watcher picks up, not a UI knob.
 */
export function VoiceSection() {
  const [voice, setVoice] = useState<VoiceSettingsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const openPanel = usePanelStore((s) => s.openPanel);

  // Re-runs on every WS (re)connection: a backend restart must replace a
  // pre-restart "Failed to fetch" with fresh data (2026-07-30).
  const wsGeneration = useWebSocketStore((s) => s.generation);
  const retryTick = useFetchRetryTick(error !== null);
  useEffect(() => {
    setError(null);
    fetchVoiceSettings()
      .then(setVoice)
      .catch((err) =>
        setError(err instanceof Error ? err.message : String(err)),
      );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wsGeneration, retryTick]);

  if (!voice) {
    return (
      <section className="settings-section">
        <h3 className="settings-section__title">Voice</h3>
        <div className="t-meta">{error ?? "(loading…)"}</div>
      </section>
    );
  }

  const phrase = `${voice.wake_word_prefix} ${voice.entity_name}`.trim();

  return (
    <section className="settings-section">
      <h3 className="settings-section__title">Voice</h3>
      <div className="settings-hint t-meta">
        Which voice speaks, and the wake phrase, live in the Identity tab.
        Wake word is currently{" "}
        <strong>{voice.wake_word_enabled ? `on — “${phrase}”` : "off"}</strong>.
      </div>
      {error && <div className="settings-error">{error}</div>}
      <div className="voice-settings-actions">
        <button
          type="button"
          className="voice-settings-save"
          onClick={() => openPanel("identity")}
        >
          open identity
        </button>
      </div>

      <div className="settings-hint t-meta">
        Timbre is not a setting — a local voice is its model file, named per
        provider in <code>providers.yaml</code>. Character is per-surface{" "}
        <code>synthesis_presets</code>; edits there trigger the config watcher
        and the next utterance picks them up.
      </div>

      {voice.style_presets.length > 0 && (
        <div className="voice-settings-row voice-settings-row--column">
          <label className="voice-settings-label">
            synthesis presets (read-only)
          </label>
          <div className="voice-settings-presets">
            {voice.style_presets.map((p) => {
              const knobs = Object.entries(p.settings);
              return (
                <div key={`${p.ref}:${p.surface}`} className="voice-settings-preset">
                  <div className="voice-settings-preset__surface t-meta">
                    {p.ref} · {p.surface}
                  </div>
                  <div className="voice-settings-preset__prompt">
                    {knobs.length > 0 ? (
                      knobs.map(([k, v]) => (
                        <span key={k} className="voice-settings-preset__knob">
                          <code>{k}</code> {String(v)}
                        </span>
                      ))
                    ) : (
                      <span className="t-meta">(empty)</span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </section>
  );
}
