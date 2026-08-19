import { create } from 'zustand';
import type {
  VoiceInstructionData,
  VoiceState,
} from '../lib/types';

/**
 * Voice store — Phase 16 S2.
 *
 * `state` mirrors the backend `voice_state` envelope plus the local
 * `listening` / `speaking` distinctions surfaced by `SttStream`. When
 * mic is hot but no speech is detected, state is `listening`; while
 * VAD reports active speech, state is `speaking_in`. Between
 * speech-end and `voice_final` arriving, state is `transcribing` (set
 * by the backend). `speaking_back` is reserved for S3 (TTS playback).
 *
 * `audioLevel` is a 0..1 RMS smoothed in `audio.ts`, fed into the
 * waveform renderer in ChatInput and the orb's listening-modulation.
 *
 * The store is intentionally tiny and doesn't subscribe to anything
 * else — `dispatch.ts` writes `state`, `HudMicButton` writes
 * `audioLevel`, and `ChatInput` / `Orb` read both.
 */

export type VoiceUiState =
  | 'idle'
  | 'listening'      // mic hot; no speech detected
  | 'speaking_in'    // VAD reports user speech in flight
  | 'transcribing'   // VAD ended; waiting for voice_final
  | 'speaking_back'; // S3: TTS playback active

/** Voice mode — global behaviour gate selected by the operator on the HUD.
 *
 * - `transcribe`: the assistant is silent. Mic captures land in the chat input
 *   for review/edit/send. Server-side TTS is suppressed even for typed
 *   messages.
 * - `command`: STT dispatches through chat_brain; assistant replies
 *   remain text/actions only.
 * - `speak`: STT + TTS. Mic captures dispatch through chat_brain;
 *   assistant replies are spoken.
 * - `terminal`: mic captures are typed into the focused Terminal pane
 *   and never reach chat_brain. Silent, like `transcribe` — the
 *   difference is only where the text lands. */
export type VoiceMode = 'transcribe' | 'command' | 'speak' | 'terminal';

interface VoiceStoreState {
  state: VoiceUiState;
  audioLevel: number;
  /** Hardware-mic gate. Toggled ONLY by the operator clicking the HUD
   * mic button. Independent of `state`, which the backend and TTS
   * player both write into freely (`speaking_back` / `idle` after a
   * voice_final / TTS-end). The HUD reads `micActive` for its on/off
   * indicator so the green-mic indicator never lies — privacy contract:
   * "mic green = mic capturing right now". */
  micActive: boolean;
  lastError: string | null;
  /** Latest `voice_instruction` from backend — the reason a reply went
   * unspoken (budget gate, or every TTS lane down). `null` until the
   * first instruction lands; kept for inspection / pulse rendering. */
  instruction: VoiceInstructionData | null;
  voiceMode: VoiceMode;
  /** When the operator commits in `transcribe` mode, the resulting
   * `voice_final` text is parked here for `ChatInput` to pull into its
   * local textarea state. Cleared by `ChatInput` after consumption. */
  pendingDictation: string | null;
  /** Browser-local interim transcript (Web Speech API) shown as grey
   * preview text while the operator speaks. Empty string when no
   * preview is active. NOT the canonical transcript — the backend's
   * `voice_final` supersedes this on commit. Cleared on speech-end. */
  partialTranscript: string;
  /** The last non-empty preview, kept after `partialTranscript` clears.
   *
   * Speech-end wipes the preview immediately, and the wake gate's verdict
   * arrives after that — so by the time a discard is known, what the
   * operator was looking at is already gone. This is the copy that
   * survives long enough to say what was not heard. */
  lastPreview: string;
  /** Held for ~2s after the wake gate refuses an utterance, then cleared.
   *
   * The operator's complaint was that a rejected utterance appears and
   * vanishes, which reads as a dead microphone. Showing nothing was the
   * rejected option: it removes the only evidence the mic heard anything.
   * An inline chat note was rejected too — it would be noise on every
   * passing conversation. This holds what was on screen, marks it as
   * not-heard, and fades.
   *
   * No score: the wake decoder hears the phrase or it does not, so there
   * is no confidence number to show and inventing one would be a figure
   * the operator could only tune against wrongly. */
  notHeard: string;
  /** True from the instant the wake phrase lands until the utterance ends.
   *
   * The counterpart to `notHeard`, and the more useful of the two: the gate
   * decides mid-sentence now, so this can say "keep going, you were heard"
   * while the operator is still talking. Before, nothing knew until they
   * stopped — which is how a minute of speech got refused a minute late. */
  woken: boolean;
  /** The engine that last actually spoke, and whether it is the one the
   * operator chose. Written from every `tts_chunk` carrying a provider —
   * the terminator chunk carries none and is skipped, so the answer
   * survives the end of the utterance rather than blanking with it.
   *
   * `engine` is resolved by the backend, never derived here: the wire's
   * `provider` is a bare catalog model id and this side has no way to map
   * one to a lane.
   *
   * The operator's rule: no silent fallback. At any moment it must be
   * possible to see WHICH engine is speaking without reading a log, and a
   * substitution must be visible as a substitution. */
  speakingLane: { engine: string; isFallback: boolean } | null;
  setState: (state: VoiceUiState) => void;
  setMicActive: (active: boolean) => void;
  setAudioLevel: (rms: number) => void;
  setError: (msg: string | null) => void;
  setInstruction: (instruction: VoiceInstructionData) => void;
  setVoiceMode: (mode: VoiceMode) => void;
  setPendingDictation: (text: string | null) => void;
  setPartialTranscript: (text: string) => void;
  /** Hold the last preview on screen, marked as not heard, then fade it. */
  markNotHeard: () => void;
  /** The wake phrase just landed, mid-utterance. */
  markWoken: () => void;
  /** The utterance resolved — the marker has said what it had to say. */
  clearWoken: () => void;
  setSpeakingLane: (engine: string, isFallback: boolean) => void;
  /** Map a backend `voice_state` envelope payload onto the UI state. */
  applyBackendState: (state: VoiceState) => void;
}

const VOICE_MODE_KEY = 'tesseract.mirror.voiceMode';
const VALID_MODES: readonly VoiceMode[] = ['transcribe', 'command', 'speak', 'terminal'] as const;

const loadVoiceMode = (): VoiceMode => {
  try {
    const v = localStorage.getItem(VOICE_MODE_KEY);
    return (VALID_MODES as readonly string[]).includes(v ?? '') ? (v as VoiceMode) : 'transcribe';
  } catch {
    return 'transcribe';
  }
};

const persistVoiceMode = (mode: VoiceMode) => {
  try {
    localStorage.setItem(VOICE_MODE_KEY, mode);
  } catch {
    /* ignore */
  }
};

/** How long a refused utterance stays on screen. Long enough to read two
 * words and understand they were not heard; short enough that it is gone
 * before the next thing you say. */
export const NOT_HEARD_MS = 2000;

export const useVoiceStore = create<VoiceStoreState>((set, get) => ({
  state: 'idle',
  audioLevel: 0,
  micActive: false,
  lastError: null,
  instruction: null,
  voiceMode: loadVoiceMode(),  // default transcribe — the assistant silent until operator opts in
  pendingDictation: null,
  partialTranscript: '',
  lastPreview: '',
  notHeard: '',
  woken: false,
  speakingLane: null,
  setState: (state) => set({ state }),
  setMicActive: (micActive) => set({ micActive }),
  setAudioLevel: (audioLevel) => set({ audioLevel }),
  setError: (lastError) => set({ lastError }),
  setInstruction: (instruction) => set({ instruction }),
  setVoiceMode: (voiceMode) => {
    persistVoiceMode(voiceMode);
    set({ voiceMode });
  },
  setPendingDictation: (pendingDictation) => set({ pendingDictation }),
  // A non-empty preview is also kept as `lastPreview`, because the clear
  // that arrives on speech-end would otherwise destroy the only thing a
  // discard has to show. Clearing does NOT touch the copy.
  setPartialTranscript: (partialTranscript) =>
    set(
      partialTranscript.trim()
        ? { partialTranscript, lastPreview: partialTranscript }
        : { partialTranscript },
    ),
  markWoken: () => set({ woken: true, notHeard: '' }),
  clearWoken: () => set({ woken: false }),
  markNotHeard: () => {
    const text = get().lastPreview.trim();
    // Nothing to hold: the browser preview is best-effort and absent when
    // Web Speech is unavailable. A marker with no words would say less than
    // the pulse row already does.
    if (!text) return;
    set({ notHeard: text });
    setTimeout(() => {
      // Only if it is still the same one. A second utterance arriving
      // inside the window owns the surface, and clearing on this timer
      // would wipe its preview instead.
      if (get().notHeard === text) set({ notHeard: '' });
    }, NOT_HEARD_MS);
  },
  setSpeakingLane: (engine, isFallback) => set({ speakingLane: { engine, isFallback } }),
  applyBackendState: (state) => {
    if (state === 'transcribing' || state === 'idle' || state === 'speaking_back') {
      set({ state });
    } else if (state === 'listening') {
      // Don't downgrade from speaking_in to listening on a backend nudge —
      // local VAD owns the speaking_in transition. Backend `listening`
      // arrives only via reset paths; honor it.
      set((s) => (s.state === 'speaking_in' ? s : { ...s, state: 'listening' }));
    }
  },
}));
