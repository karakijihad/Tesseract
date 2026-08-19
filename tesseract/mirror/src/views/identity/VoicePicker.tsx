import { useState } from "react";
import { Block } from "../../components/common/Block";
import { Note } from "../../components/common/Note";

import { ensureTtsPlayer } from "../../stores/dispatch/tts";
import { useToastStore } from "../../stores/toasts";
import { useCachedFetch } from "../../lib/useCachedFetch";
import {
  fetchVoiceCatalog,
  postVoicePrimary,
  postVoiceTest,
  type CatalogVoice,
  type VoiceCatalogResponse,
} from "../../lib/api";
import { Button } from "../../components/common/Button";
import { MenuItem } from "../../components/common/MenuItem";

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
/** What a voice costs per hour of speech, which is how spend is estimated:
 * speech length × this rate. Zero is free. */
function costLabel(v: CatalogVoice): string {
  return v.cost_per_hour_usd
    ? `$${v.cost_per_hour_usd.toFixed(2)}/hour spoken`
    : "free";
}

/** The one thing the row says about its own state, in precedence order.
 *
 * Early returns rather than a ternary chain: the order IS the behaviour —
 * `locked` sitting above `isPrimary` is what makes a keyless current voice
 * read "needs GOOGLE_API_KEY" instead of "speaking", which is the true
 * statement. That decision should be legible as a sequence, not recovered
 * from nesting depth.
 */
function stateLabel(
  v: CatalogVoice,
  { locked, saving, isPrimary, fallbackAt }: {
    locked: boolean;
    saving: boolean;
    isPrimary: boolean;
    fallbackAt: number;
  },
): string {
  if (!v.enabled) return "disabled in providers.yaml";
  if (locked) return `needs ${v.key_env}`;
  if (saving) return "saving…";
  if (isPrimary) return "speaking";
  if (fallbackAt >= 0) return `fallback ${fallbackAt + 1}`;
  return "";
}

export function VoicePicker() {
  // `useCachedFetch` rather than a fetch of its own: it already carries the
  // WS-generation refetch and the error retry this had hand-rolled, and it
  // adds the one thing they did not — the catalog survives the unmount, so
  // returning to this panel paints the voices instead of `(loading…)`.
  const {
    data: catalog,
    error,
    set: setCatalog,
    setError,
  } = useCachedFetch<VoiceCatalogResponse>(
    "identity.voice-catalog",
    fetchVoiceCatalog,
  );
  const [busyRef, setBusyRef] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);

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
      // Both surfaces, and that is deliberate: the inline note is next to
      // the button that failed, and the toast reaches an operator who has
      // already scrolled away or switched tabs while a cloud voice was
      // still rendering. `ApiError`'s message now carries the provider's
      // own explanation, so this says which key or which network failed
      // rather than "synthesis_failed".
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg);
      useToastStore.getState().push(`Voice sample failed — ${msg}`, "error");
    } finally {
      setPlaying(false);
    }
  };

  if (!catalog) {
    return (
      <Block title={null}>
        <div className="t-meta">{error ?? "(loading…)"}</div>
      </Block>
    );
  }

  return (
    <Block title={null}>
      <div className="identity-panel-body">
        {error && <Note tone="bad">{error}</Note>}

        <ul className="voice-picker-list">
          {catalog.voices.map((v) => {
            const isPrimary = v.ref === catalog.primary;
            const fallbackAt = catalog.fallbacks.indexOf(v.ref);
            const locked = !v.key_present;
            return (
              <li key={v.ref}>
                <MenuItem
                  className="voice-picker-row"
                  active={isPrimary}
                  disabled={!v.enabled || locked || busyRef !== null || isPrimary}
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
                  {/* Said on the row that would spend it, not on the spend
                      view afterwards — a paid voice should announce itself
                      at the moment it is chosen. */}
                  <span className="t-meta voice-picker-cost">{costLabel(v)}</span>
                  <span className="t-meta voice-picker-ref">{v.ref}</span>
                  <span className="t-meta voice-picker-state">
                    {stateLabel(v, {
                      locked,
                      saving: busyRef === v.ref,
                      isPrimary,
                      fallbackAt,
                    })}
                  </span>
                </MenuItem>
              </li>
            );
          })}
        </ul>

        <div className="identity-actions">
          <Button
            onClick={() => void play()}
            disabled={playing || busyRef !== null}
          >
            {playing ? "speaking…" : "play sample"}
          </Button>
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
    </Block>
  );
}
