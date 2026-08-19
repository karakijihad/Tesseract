import { useCallback, useEffect, useRef, useState } from "react";

import { Button } from "../../components/common/Button";
import { Checkbox } from "../../components/common/Checkbox";
import { Input } from "../../components/common/Input";
import { Note } from "../../components/common/Note";
import {
  deleteWakeCalibration,
  fetchWakeStatus,
  postWakeCalibration,
  saveIdentity,
  type WakeCalibrateResult,
  type WakeStatus,
} from "../../lib/api";
import { WakeRecorder, type Take } from "../../lib/voice/wake-recorder";

/** The wake word: whether it is on, what it is, and checking it hears you.
 *
 * **Nothing here trains anything.** The spotter can hear a phrase it has
 * never been shown, so this run does not teach it your voice — it finds the
 * tightest sensitivity at which every take of yours fires and your ordinary
 * speech does not, and records that. It is re-runnable without limit, and
 * "forget" returns the gate to passing everything through.
 *
 * The run is guided rather than a set of buttons. Eight takes pressed one at
 * a time is eight chances to hold the microphone wrong, and a screen that
 * asks "record another?" after each one makes the operator responsible for a
 * count the system already knows. So each take is a fixed window that ends
 * itself, the next begins on its own, and the run submits when it has what it
 * needs.
 *
 * The second half asks you to READ three lines rather than "talk normally".
 * Told to improvise, people say a sentence or two and stop; those recordings
 * are what prove the setting separates the phrase from your ordinary speech,
 * so they cannot be the part that gets skimped. Reading is also repeatable —
 * a re-run checks against comparable speech instead of whatever came to mind
 * that time.
 *
 * No recording is kept. Each take is sent, decoded, and dropped; what
 * persists is the phrase and two numbers.
 */

const PHRASE_TAKES = 5;

/** Neutral lines to read for the negative half. Deliberately ordinary — the
 * point is to sound like you do when you are NOT addressing the assistant,
 * which is the speech the gate has to sit above. Any line containing the
 * entity's name is filtered out before use, since a negative that says the
 * name is not a negative. */
const SENTENCE_POOL = [
  "The kettle boiled twice before anyone noticed.",
  "Tuesday's meeting moved to the smaller room.",
  "There are three boxes left in the hallway.",
  "The train was late again this morning.",
  "Someone repainted the fence over the weekend.",
  "The recipe calls for twice as much butter.",
  "Nothing on the calendar until Thursday afternoon.",
  "I left the blue folder on the kitchen table.",
  "It rained for most of the afternoon.",
  "The battery lasts about six hours now.",
];
const SENTENCE_TAKES = 3;

function pickSentences(name: string): string[] {
  const needle = name.trim().toLowerCase();
  const usable = SENTENCE_POOL.filter(
    (s) => !needle || !s.toLowerCase().includes(needle),
  );
  const shuffled = [...usable].sort(() => Math.random() - 0.5);
  return shuffled.slice(0, SENTENCE_TAKES);
}

type Phase = "idle" | "phrase" | "sentences" | "submitting";

export function WakeWordSection() {
  const [status, setStatus] = useState<WakeStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [done, setDone] = useState(0);
  const [level, setLevel] = useState(0);
  const [sentences, setSentences] = useState<string[]>([]);
  const [result, setResult] = useState<WakeCalibrateResult | null>(null);
  const [prefix, setPrefix] = useState("");
  const [savingSwitch, setSavingSwitch] = useState(false);
  // The VAD decides when the operator started; until then the screen
  // says it is waiting rather than implying it is capturing.
  const [speaking, setSpeaking] = useState(false);

  const recorder = useRef<WakeRecorder | null>(null);
  // Set when the operator cancels or the section unmounts. Checked between
  // takes: a run that keeps advancing after the screen is gone would hold the
  // microphone and then post recordings nobody asked for.
  const abandoned = useRef(false);

  const refresh = useCallback(
    () =>
      fetchWakeStatus()
        .then((s) => {
          setStatus(s);
          setPrefix((p) => p || s.phrase.split(" ")[0] || "hey");
        })
        .catch((e) => setError(String(e))),
    [],
  );

  useEffect(() => {
    void refresh();
    return () => {
      abandoned.current = true;
      recorder.current?.dispose();
    };
  }, [refresh]);

  const setEnabled = useCallback(
    async (on: boolean) => {
      setSavingSwitch(true);
      setError(null);
      try {
        await saveIdentity({ wake_word: { enabled: on } });
        await refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setSavingSwitch(false);
      }
    },
    [refresh],
  );

  const savePrefix = useCallback(async () => {
    const next = prefix.trim();
    if (!next) return;
    setError(null);
    try {
      await saveIdentity({ wake_word: { prefix: next } });
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [prefix, refresh]);

  /** The whole run: five takes of the phrase, three read lines, then submit.
   *
   * One function rather than a state machine driven by clicks, because the
   * operator's only decisions are "start" and "stop" — everything between is
   * bookkeeping the screen should be doing for them.
   */
  const check = useCallback(async () => {
    if (!status) return;
    abandoned.current = false;
    setError(null);
    setResult(null);
    recorder.current ??= new WakeRecorder();
    const lines = pickSentences(status.phrase.split(" ").slice(1).join(" "));
    setSentences(lines);

    const phraseTakes: Take[] = [];
    const speechTakes: Take[] = [];

    const runOne = async (): Promise<Take | null> => {
      setSpeaking(false);
      try {
        return await recorder.current!.record(setLevel, () => setSpeaking(true));
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        return null;
      } finally {
        setLevel(0);
        setSpeaking(false);
      }
    };
    // A beat between takes. Without it the next window opens while the
    // operator is still finishing the last one, and take two records the tail
    // of take one.
    const beat = () => new Promise((r) => setTimeout(r, 700));

    setPhase("phrase");
    for (let i = 0; i < PHRASE_TAKES; i++) {
      if (abandoned.current) return;
      setDone(i);
      const take = await runOne();
      if (!take) return setPhase("idle");
      phraseTakes.push(take);
      setDone(i + 1);
      if (i < PHRASE_TAKES - 1) await beat();
    }

    setPhase("sentences");
    for (let i = 0; i < lines.length; i++) {
      if (abandoned.current) return;
      setDone(i);
      await beat();
      const take = await runOne();
      if (!take) return setPhase("idle");
      speechTakes.push(take);
      setDone(i + 1);
    }

    if (abandoned.current) return;
    setPhase("submitting");
    try {
      const res = await postWakeCalibration({
        phrase_clips: phraseTakes.map((t) => t.audio_b64),
        speech_clips: speechTakes.map((t) => t.audio_b64),
      });
      setResult(res);
      if (res.status) setStatus(res.status);
      else await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPhase("idle");
      setDone(0);
    }
  }, [status, refresh]);

  const cancel = useCallback(() => {
    abandoned.current = true;
    recorder.current?.stop();
    recorder.current?.dispose();
    setPhase("idle");
    setDone(0);
    setLevel(0);
  }, []);

  const forget = useCallback(async () => {
    setError(null);
    try {
      const res = await deleteWakeCalibration();
      setStatus(res.status);
      setResult(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  if (!status) {
    return (
      <section className="settings-section">
        <div className="t-meta">{error ?? "(loading…)"}</div>
      </section>
    );
  }

  const running = phase !== "idle";

  return (
    <section className="settings-section">
      <StateLine status={status} />

      <div className="identity-field">
        <span className="identity-field-label t-meta">wake word</span>
        <div className="identity-wake-row">
          <Checkbox
            id="wake-enabled"
            checked={status.enabled}
            disabled={savingSwitch || running}
            onChange={(on) => void setEnabled(on)}
            ariaLabel="wake word enabled"
          />
          <Input
            className="identity-input identity-input--short"
            value={prefix}
            maxLength={40}
            disabled={running}
            ariaLabel="wake word prefix"
            onChange={setPrefix}
            onKeyDown={(e) => e.key === "Enter" && void savePrefix()}
          />
          <span className="t-meta">
            The phrase is these two words — “{status.phrase}”. The second is the
            name, on the Identity tab.
          </span>
        </div>
      </div>

      {!status.models_present && (
        <Note tone="warn">
          The wake-word models are not installed yet. They arrive with the
          listening models — reinstall those from Capabilities and this can
          record.
        </Note>
      )}

      {status.stale && (
        <Note tone="warn">
          Checked for “{status.calibrated_for}”. What was confirmed is that
          those two words are heard reliably, so a rename cannot carry it over
          — run it again for “{status.phrase}”.
        </Note>
      )}

      {running ? (
        <Running
          speaking={speaking}
          phase={phase}
          done={done}
          level={level}
          sentences={sentences}
          onCancel={cancel}
        />
      ) : (
        <div className="identity-actions">
          <Button
            onClick={() => void check()}
            disabled={!status.models_present || !status.enabled}
          >
            {status.calibrated ? "check again" : "check it hears you"}
          </Button>
          {status.calibrated && <Button onClick={() => void forget()}>forget</Button>}
        </div>
      )}

      {!running && !status.enabled && (
        <span className="t-meta">Turn it on to check it.</span>
      )}

      {error && <Note tone="bad">{error}</Note>}
      {result && !running && <Result result={result} />}
    </section>
  );
}

function StateLine({ status }: { status: WakeStatus }) {
  // Three states said as three things. The middle one is the one that would
  // otherwise be silently wrong: switched on is not the same as listening.
  if (!status.enabled)
    return <Note>Off — every utterance dispatches.</Note>;
  if (!status.armed)
    return (
      <Note tone="warn">
        On, but not checked yet — so every utterance still dispatches. Say the
        phrase below, watch it land, and it starts filtering.
      </Note>
    );
  return (
    <Note>
      Listening for “{status.phrase}”. Only that starts a turn — confirmed at{" "}
      {status.threshold?.toFixed(2)} across {status.samples} takes.
    </Note>
  );
}

function Running({
  speaking,
  phase,
  done,
  level,
  sentences,
  onCancel,
}: {
  speaking: boolean;
  phase: Phase;
  done: number;
  level: number;
  sentences: string[];
  onCancel: () => void;
}) {
  const meter = (
    <span
      className="wake-level"
      style={{ ["--wake-level" as string]: Math.min(1, level * 4).toFixed(3) }}
      aria-label="input level"
    />
  );

  if (phase === "submitting") {
    return (
      <div className="identity-field">
        <span className="t-meta">Measuring…</span>
      </div>
    );
  }

  return (
    <div className="identity-field">
      {phase === "phrase" ? (
        <>
          <span className="wake-prompt t-body">Say the phrase</span>
          <span className="t-meta">
            take {Math.min(done + 1, PHRASE_TAKES)} of {PHRASE_TAKES} —{" "}
            {speaking ? "listening…" : "waiting for you"}
          </span>
        </>
      ) : (
        <>
          <span className="wake-prompt t-body">{sentences[done] ?? ""}</span>
          <span className="t-meta">
            read it aloud — line {Math.min(done + 1, sentences.length)} of{" "}
            {sentences.length} — {speaking ? "listening…" : "waiting for you"}
          </span>
        </>
      )}
      <div className="identity-wake-row">
        {meter}
        <Button onClick={onCancel}>stop</Button>
      </div>
    </div>
  );
}

function Result({ result }: { result: WakeCalibrateResult }) {
  if (!result.ok) {
    return (
      <Note tone="warn">
        <strong>Not confirmed.</strong> {result.reason}
      </Note>
    );
  }
  return (
    <Note>
      <strong>Confirmed.</strong> Heard in all {result.phrase_takes} takes at{" "}
      {result.threshold.toFixed(2)}, and not once across {result.speech_takes}{" "}
      recordings of ordinary speech. Live on your next utterance.
    </Note>
  );
}
