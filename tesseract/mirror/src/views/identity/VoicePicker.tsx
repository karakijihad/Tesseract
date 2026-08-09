import { useEffect, useState } from "react";

import { ensureTtsPlayer } from "../../stores/dispatch/tts";
import { useToastStore } from "../../stores/toasts";
import { useWebSocketStore } from "../../stores/websocket";
import { useFetchRetryTick } from "../../lib/useFetchRetry";
import {
  fetchVoiceCatalog,
  postVoicePrimary,
  postVoiceTest,
  type VoiceCatalogResponse,
} from "../../lib/api";

/** Which voice speaks.
 *
 * The list is the catalog — every `kind: tts` entry `providers.yaml`
 * holds. Adding a voice there grows this picker with no code change, and
 * nothing here names a provider.
 *
 * A pick writes `roles.yaml::voice.tts.primary` and rebuilds the voice
 * runtime, so it takes effect on the next utterance without a restart.
 * The displaced voice becomes the first fallback rather than being
 * dropped — the lane the operator was just using stays the thing that
 * speaks when the new one fails.
 *
 * Auditioning plays through the same `TtsPlayer` a spoken turn uses, so
 * what you hear is the real path (and the orb lights up for it). There is
 * no per-call voice override by design — AS-3 removed it — which is why
 * the sample always speaks the *current* selection.
 */
export function VoicePicker() {
  const [catalog, setCatalog] = useState<VoiceCatalogResponse | null>(null);
  const [busyRef, setBusyRef] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const wsGeneration = useWebSocketStore((s) => s.generation);
  const retryTick = useFetchRetryTick(error !== null);
  useEffect(() => {
    setError(null);
    fetchVoiceCatalog()
      .then(setCatalog)
      .catch((err) =>
        setError(err instanceof Error ? err.message : String(err)),
      );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wsGeneration, retryTick]);

  const pick = async (ref: string) => {
    if (!catalog || ref === catalog.primary) return;
    setBusyRef(ref);
    setError(null);
    try {
      const res = await postVoicePrimary(ref);
      setCatalog({ ...catalog, primary: res.primary, fallbacks: res.fallbacks });
      if (res.live_update_failed) {
        // The YAML landed; only the hot rebuild didn't. Say which, rather
        // than reporting a success the running process isn't honouring.
        useToastStore
          .getState()
          .push(
            `Voice saved, but the live rebuild failed — restart to apply. ${res.live_update_error ?? ""}`,
            "warning",
          );
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyRef(null);
    }
  };

  const play = async () => {
    setPlaying(true);
    setError(null);
    try {
      const res = await postVoiceTest();
      // `is_final: true` so the player flips back to idle when the one
      // buffer drains — an audition is a complete utterance, not a chunk
      // of a turn still streaming.
      await ensureTtsPlayer().play({ audio_b64: res.audio_b64, is_final: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPlaying(false);
    }
  };

  if (!catalog) {
    return (
      <section className="identity-view-card identity-panel">
        <div className="identity-view-card-heading t-meta">Voice</div>
        <div className="identity-panel-body">
          <div className="t-meta">{error ?? "(loading…)"}</div>
        </div>
      </section>
    );
  }

  return (
    <section className="identity-view-card identity-panel">
      <div className="identity-view-card-heading t-meta">Voice</div>
      <div className="identity-panel-body">
        {error && <div className="settings-error">{error}</div>}

        <ul className="voice-picker-list">
          {catalog.voices.map((v) => {
            const isPrimary = v.ref === catalog.primary;
            const fallbackAt = catalog.fallbacks.indexOf(v.ref);
            return (
              <li key={v.ref}>
                <button
                  type="button"
                  className={`voice-picker-row${isPrimary ? " is-primary" : ""}`}
                  disabled={!v.enabled || busyRef !== null || isPrimary}
                  onClick={() => void pick(v.ref)}
                >
                  <span className="voice-picker-mark" aria-hidden="true">
                    {isPrimary ? "●" : "○"}
                  </span>
                  <span className="voice-picker-name">
                    {v.label || v.ref}
                    {v.gender && (
                      <span className="t-meta voice-picker-gender">{` · ${v.gender}`}</span>
                    )}
                  </span>
                  <span className="t-meta voice-picker-ref">{v.ref}</span>
                  <span className="t-meta voice-picker-state">
                    {!v.enabled
                      ? "disabled in providers.yaml"
                      : busyRef === v.ref
                        ? "saving…"
                        : isPrimary
                          ? "speaking"
                          : fallbackAt >= 0
                            ? `fallback ${fallbackAt + 1}`
                            : ""}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>

        <div className="identity-actions">
          <button
            type="button"
            className="identity-save"
            onClick={() => void play()}
            disabled={playing || busyRef !== null}
          >
            {playing ? "speaking…" : "play sample"}
          </button>
          <span className="t-meta">“{catalog.sample_text}”</span>
        </div>
        <span className="t-meta identity-field-hint">
          The sample speaks the selected voice — pick first, then play. Edit
          the line at <code>voice.test_sample</code> in <code>mirror.yaml</code>.
          Character (pace, variability) is per-surface{" "}
          <code>synthesis_presets</code> on the catalog entry; there is no
          timbre knob, because a local voice is its model file.
        </span>
      </div>
    </section>
  );
}
