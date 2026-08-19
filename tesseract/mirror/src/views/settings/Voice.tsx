import { useState } from "react";

import { Note } from "../../components/common/Note";
import {
  fetchVoiceSettings,
  postVoicePreset,
  type VoiceSettingsResponse,
  type VoiceStylePreset,
} from "../../lib/api";
import { useCachedFetch } from "../../lib/useCachedFetch";
import { Input } from "../../components/common/Input";
import { Button } from "../../components/common/Button";
import { VoicePicker } from "../identity/VoicePicker";
import { WakeWordSection } from "./WakeWord";
import { Hint } from "../../components/ui/Hint";

/** What speech sounds like.
 *
 * The two controls that used to live here moved to the Identity tab in
 * AS-5: the wake phrase (it is `<prefix> <name>`, so it belongs beside
 * the name) and the voice itself. What remains is the character each
 * surface renders — per-provider `synthesis_presets`.
 *
 * Those were read-only until the editor below, and the reason they are
 * editable WITH CAUTION is that the lanes do not share knobs: the local
 * voice takes two numbers, the cloud lane takes two lines of prose, and
 * neither reads the other's. So the fields come from the backend's own
 * per-adapter table rather than from a shape assumed here, and a lane with
 * no entry in it renders read-only instead of offering a control that
 * writes nowhere.
 *
 * This is an operator surface. No tool reaches it — a preset the assistant
 * could write would be the per-utterance tone control the voice contract
 * refuses.
 */
export function VoiceSection() {
  const {
    data: voice,
    error,
    set,
    setError,
  } = useCachedFetch<VoiceSettingsResponse>(
    "settings.voice",
    fetchVoiceSettings,
  );
  // Keyed by `${ref}:${surface}` so two surfaces of one lane edit
  // independently; absent means "showing what config holds".
  const [drafts, setDrafts] = useState<
    Record<string, Record<string, string>>
  >({});
  const [saving, setSaving] = useState<string | null>(null);

  if (!voice) {
    return (
      <section className="settings-section">
        <div className="t-meta">{error ?? "(loading…)"}</div>
      </section>
    );
  }

  const keyOf = (p: VoiceStylePreset) => `${p.ref}:${p.surface}`;

  // `?? {}` on both, and not defensively: a backend one release behind this
  // screen ships a preset with no `knobs` at all, and `Object.keys(undefined)`
  // threw hard enough to blank the app before RailView had a boundary.
  const knobsOf = (p: VoiceStylePreset) => p.knobs ?? {};
  const settingsOf = (p: VoiceStylePreset) => p.settings ?? {};

  const draftFor = (p: VoiceStylePreset): Record<string, string> =>
    drafts[keyOf(p)] ??
    Object.fromEntries(
      Object.keys(knobsOf(p)).map((k) => [k, String(settingsOf(p)[k] ?? "")]),
    );

  const edit = (p: VoiceStylePreset, knob: string, value: string) =>
    setDrafts((d) => ({
      ...d,
      [keyOf(p)]: { ...draftFor(p), [knob]: value },
    }));

  async function save(
    p: VoiceStylePreset,
    settings: Record<string, string | number> | null,
  ) {
    setSaving(keyOf(p));
    setError(null);
    try {
      set(await postVoicePreset({ ref: p.ref, surface: p.surface, settings }));
      // Dropped, not kept: what comes back is what config now holds, and a
      // surviving draft would go on showing the value the route refused.
      setDrafts((d) => {
        const next = { ...d };
        delete next[keyOf(p)];
        return next;
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(null);
    }
  }

  // Every preset the operator has moved off its shipped character. `reset all`
  // exists because these are the only way a NEW shipped default reaches an
  // install: config seeding adds keys and never rewrites a value you already
  // have, so an improved speed or style stays unread until it is reset to.
  const overridden = voice.style_presets.filter(
    (p) => p.overridden && Object.keys(knobsOf(p)).length > 0,
  );

  async function resetAll() {
    setSaving("*");
    setError(null);
    try {
      // Independent writes to different refs — the route takes one preset at a
      // time, so they go together rather than in a chain. `allSettled` so one
      // refusal does not abandon the rest; the last response wins as the fresh
      // snapshot, and a failure surfaces through the same error line.
      const results = await Promise.allSettled(
        overridden.map((p) =>
          postVoicePreset({ ref: p.ref, surface: p.surface, settings: null }),
        ),
      );
      const ok = results.filter(
        (r): r is PromiseFulfilledResult<VoiceSettingsResponse> =>
          r.status === "fulfilled",
      );
      // One re-read after everything has settled, NOT the last response in
      // input order. Each write's response is a re-read taken immediately
      // after that write, so the last one dispatched can be an older snapshot
      // than a sibling that finished after it — leaving the pane showing an
      // override that is no longer on disk.
      if (ok.length > 0) set(await fetchVoiceSettings());
      setDrafts({});
      const failed = results.length - ok.length;
      if (failed > 0) {
        setError(`${failed} of ${results.length} presets could not be reset`);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(null);
    }
  }

  return (
    <section className="settings-section">
      {error && <Note tone="bad">{error}</Note>}

      {/* Everything about the voice is here now: which one speaks, the wake
          word and its training. It used to be split with the Identity tab,
          which meant "how it sounds" and "what wakes it" were two journeys
          for one subject. Identity keeps the names and links across. */}
      <VoicePicker />
      <WakeWordSection />

      <Note>
        Timbre is not a setting — a local voice is its model file, named per
        provider in <code>providers.yaml</code>. Character is the per-surface{" "}
        <code>synthesis_presets</code> below: <code>intent</code> is the short
        acknowledgement, <code>answer</code> is the spoken reply. Saving one
        takes effect on the next utterance.
      </Note>

      {voice.style_presets.length > 0 && (
        <div className="voice-settings-row voice-settings-row--column">
          <div className="voice-settings-heading">
            <label className="voice-settings-label">synthesis presets</label>
            <Hint
              label={
                overridden.length > 0
                  ? `Put speed, style and every other knob back to what ${voice.entity_name || "this build"} ships with`
                  : "Every preset is already on its shipped value"
              }
            >
              <Button
                onClick={() => void resetAll()}
                disabled={saving !== null || overridden.length === 0}
              >
                {saving === "*"
                  ? "resetting…"
                  : `reset all${overridden.length > 0 ? ` (${overridden.length})` : ""}`}
              </Button>
            </Hint>
          </div>
          <div className="voice-settings-presets">
            {voice.style_presets.map((p) => {
              const knobs = Object.entries(knobsOf(p));
              const draft = draftFor(p);
              const busy = saving === keyOf(p);
              const changed = knobs.some(
                ([k]) => draft[k] !== String(settingsOf(p)[k] ?? ""),
              );
              return (
                <div key={keyOf(p)} className="voice-settings-preset">
                  <div className="voice-settings-preset__surface t-meta">
                    {p.ref} · {p.surface}
                  </div>

                  {knobs.length === 0 ? (
                    // No editable knobs for this adapter. Shown as it is
                    // rather than as an input, because a field that saves a
                    // value the lane never reads is worse than no field.
                    <div className="voice-settings-preset__prompt">
                      {Object.entries(settingsOf(p)).map(([k, v]) => (
                        <span key={k} className="voice-settings-preset__knob">
                          <code>{k}</code> {String(v)}
                        </span>
                      ))}
                    </div>
                  ) : (
                    knobs.map(([name, knob]) => (
                      <div key={name} className="voice-settings-knob">
                        <label
                          className="voice-settings-knob__name t-meta"
                          htmlFor={`${keyOf(p)}:${name}`}
                        >
                          {name}
                        </label>
                        {knob.kind === "number" ? (
                          <Input
                            id={`${keyOf(p)}:${name}`}
                            className="voice-settings-input"
                            type="number"
                            min={knob.min}
                            max={knob.max}
                            // A UI affordance, not a contract: the backend
                            // validates the range and accepts anything in it.
                            step={0.05}
                            value={draft[name] ?? ""}
                            onChange={(v) => edit(p, name, v)}
                          />
                        ) : (
                          <Input
                            id={`${keyOf(p)}:${name}`}
                            className="voice-settings-input voice-settings-input--wide"
                            maxLength={knob.max_chars}
                            value={draft[name] ?? ""}
                            onChange={(v) => edit(p, name, v)}
                          />
                        )}
                      </div>
                    ))
                  )}

                  {knobs.length > 0 && (
                    <div className="voice-settings-actions">
                      {p.overridden && (
                        <Button
                          onClick={() => save(p, null)}
                          disabled={busy}
                        >
                          reset
                        </Button>
                      )}
                      <Button
                        onClick={() =>
                          save(
                            p,
                            Object.fromEntries(
                              knobs.map(([name, knob]) => [
                                name,
                                knob.kind === "number"
                                  ? Number(draft[name])
                                  : draft[name] ?? "",
                              ]),
                            ),
                          )
                        }
                        disabled={busy || !changed}
                      >
                        {busy ? "saving…" : "save"}
                      </Button>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </section>
  );
}
