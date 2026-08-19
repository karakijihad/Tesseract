import { useEffect, useState } from "react";

import { Block } from "../../components/common/Block";
import { Note } from "../../components/common/Note";

import { useIdentityStore } from "../../stores/identity";
import { useWebSocketStore } from "../../stores/websocket";
import { useFetchRetryTick } from "../../lib/useFetchRetry";
import { fetchIdentity, fetchVoiceSettings, saveIdentity } from "../../lib/api";
import type { IdentitySavePatch } from "../../lib/types";
import { Input } from "../../components/common/Input";
import { Button } from "../../components/common/Button";

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
  const [saved, setSaved] = useState<{
    name: string;
    operator: string;
  } | null>(null);
  const [saving, setSaving] = useState(false);
  // Read-only here: the phrase is half this name, so the card shows what
  // it currently is. Editing it lives with the rest of voice.
  const [wakePrefixText, setWakePrefixText] = useState("hey");
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
        setWakePrefixText(voice.wake_word_prefix);
        setSaved({
          name: ident.name,
          operator: ident.operator_name,
        });
      })
      .catch((err) =>
        setError(err instanceof Error ? err.message : String(err)),
      );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wsGeneration, retryTick]);

  const trimmedName = name.trim();
  const trimmedOperator = operator.trim();
  const dirty =
    saved !== null &&
    (trimmedName !== saved.name ||
      trimmedOperator !== saved.operator);
  // The backend refuses a blank name or prefix; disabling save says so
  // before the round trip instead of surfacing a 400 for a typo.
  const valid = trimmedName !== "" && trimmedOperator !== "";

  const save = async () => {
    if (!saved || !dirty || !valid) return;
    const patch: IdentitySavePatch = {};
    if (trimmedName !== saved.name) patch.name = trimmedName;
    if (trimmedOperator !== saved.operator) patch.operator_name = trimmedOperator;
    setSaving(true);
    setError(null);
    try {
      const applied = await saveIdentity(patch);
      setSaved({
        name: applied.name,
        operator: applied.operator_name,
      });
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

  const phrase = `${wakePrefixText} ${trimmedName}`.trim();

  return (
    <Block title={null}>
      <div className="identity-panel-body">
        {error && <Note tone="bad">{error}</Note>}

        <div className="identity-field">
          <label className="identity-field-label t-meta" htmlFor="identity-name">
            name
          </label>
          <Input
            id="identity-name"
            className="identity-input"
            value={name}
            maxLength={40}
            disabled={saved === null}
            onChange={setName}
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
          <Input
            id="identity-operator"
            className="identity-input"
            value={operator}
            maxLength={40}
            disabled={saved === null}
            onChange={setOperator}
            onKeyDown={(e) => e.key === "Enter" && void save()}
          />
          <span className="t-meta identity-field-hint">
            Your display name in chat bubbles and greetings.
          </span>
        </div>

        {/* Voice lives in Settings → Voice, all of it: which voice speaks,
            the wake word, and training it. The name above is half the wake
            phrase, which is why this points across rather than staying
            silent — but splitting one subject across two tabs is what made
            "how it sounds" and "what wakes it" two separate journeys. */}
        <div className="identity-field">
          <span className="identity-field-label t-meta">voice</span>
          <span className="t-meta">
            How it sounds and what wakes it are in Settings → Voice. The wake
            phrase is “{phrase}” — the second word is the name above, so
            renaming changes it.
          </span>
        </div>

        <div className="identity-actions">
          <Button
            onClick={() => void save()}
            disabled={!dirty || !valid || saving}
          >
            {saving ? "saving…" : "save"}
          </Button>
          {dirty && !valid && (
            <span className="t-meta">Name, operator and prefix cannot be blank.</span>
          )}
        </div>
      </div>
    </Block>
  );
}
