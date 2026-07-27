import { useEffect, useState } from 'react';

import { Hint } from '../../components/ui/Hint';
import {
  fetchVoiceSettings,
  postVoiceSettings,
  type VoiceSettingsResponse,
} from '../../lib/api';

export function VoiceSection() {
  const [voice, setVoice] = useState<VoiceSettingsResponse | null>(null);
  const [voiceId, setVoiceId] = useState('Charon');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchVoiceSettings()
      .then((res) => {
        setVoice(res);
        setVoiceId(res.voice_id || 'Charon');
      })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, []);

  const dirty = voice !== null && voiceId !== voice.voice_id;

  const save = async () => {
    if (!voice) return;
    setSaving(true);
    setError(null);
    try {
      await postVoiceSettings({ voice_id: voiceId });
      const fresh = await fetchVoiceSettings();
      setVoice(fresh);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  if (!voice) {
    return (
      <section className="settings-section">
        <h3 className="settings-section__title">Voice</h3>
        <div className="t-meta">{error ?? '(loading…)'}</div>
      </section>
    );
  }

  return (
    <section className="settings-section">
      <h3 className="settings-section__title">Voice</h3>
      <div className="settings-hint t-meta">
        Timbre is the only knob exposed here. Style/character is locked to
        per-surface presets in <code>roles.yaml</code> (
        <code>voice.tts.settings.api.google.gemini_flash_tts.synthesis_presets</code>
        ) — edits there trigger the config watcher and the next utterance
        picks them up. Saving timbre also reloads the voice runtime.
      </div>
      {error && <div className="settings-error">{error}</div>}
      <div className="voice-settings-row">
        <label className="voice-settings-label">voice id</label>
        <select
          className="voice-settings-input"
          value={voiceId}
          onChange={(e) => setVoiceId(e.target.value)}
        >
          {voice.available_voice_ids.map((id) => (
            <option key={id} value={id}>{id}</option>
          ))}
        </select>
        <Hint label="Cloud provider: Gemini Flash TTS (single GOOGLE_API_KEY).">
          <span className="voice-settings-provider t-meta">Gemini Flash TTS</span>
        </Hint>
      </div>
      {voice.gemini_style_presets.length > 0 && (
        <div className="voice-settings-row voice-settings-row--column">
          <label className="voice-settings-label">style presets (read-only)</label>
          <div className="voice-settings-presets">
            {voice.gemini_style_presets.map((p) => (
              <div key={p.surface} className="voice-settings-preset">
                <div className="voice-settings-preset__surface t-meta">
                  {p.surface}
                </div>
                <div className="voice-settings-preset__prompt">
                  {p.style_prompt || <span className="t-meta">(empty)</span>}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
      <div className="voice-settings-actions">
        <button
          type="button"
          className="voice-settings-save"
          onClick={save}
          disabled={!dirty || saving}
        >
          {saving ? 'saving…' : 'save'}
        </button>
      </div>
    </section>
  );
}
