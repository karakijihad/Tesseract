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
  setState: (state: VoiceUiState) => void;
  setMicActive: (active: boolean) => void;
  setAudioLevel: (rms: number) => void;
  setError: (msg: string | null) => void;
  setInstruction: (instruction: VoiceInstructionData) => void;
  setVoiceMode: (mode: VoiceMode) => void;
  setPendingDictation: (text: string | null) => void;
  setPartialTranscript: (text: string) => void;
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

export const useVoiceStore = create<VoiceStoreState>((set) => ({
  state: 'idle',
  audioLevel: 0,
  micActive: false,
  lastError: null,
  instruction: null,
  voiceMode: loadVoiceMode(),  // default transcribe — the assistant silent until operator opts in
  pendingDictation: null,
  partialTranscript: '',
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
  setPartialTranscript: (partialTranscript) => set({ partialTranscript }),
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
