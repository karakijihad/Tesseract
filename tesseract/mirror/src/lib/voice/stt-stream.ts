/**
 * SttStream — orchestrator that ties `audio.ts` (PCM capture) + `vad.ts`
 * (Silero speech-end detection) together and ships frames over the WS.
 *
 * Lifecycle:
 *   start()  → AudioCapture starts; VAD attaches to the same stream
 *              with `getStream` returning the captured MediaStream.
 *              Each PCM frame is forwarded over WS binary; RMS level is
 *              broadcast through `onLevel`. Mic state goes to
 *              `listening` until VAD reports speech.
 *   speech-start (VAD) → state flips to `speaking`. Frames continue
 *                        streaming; backend buffer accumulates.
 *   speech-end (VAD)   → send `voice_commit`; browser partials are
 *                        preview-only. Backend local Whisper is the
 *                        authoritative final transcript, with cloud
 *                        fallback. Local state flips back to `listening`
 *                        so a follow-up utterance is captured cleanly.
 *   stop()   → if mid-utterance, send `voice_cancel`. Tear down
 *              capture + VAD; release mic.
 *
 * The class is constructed once and reused across mic toggles. The
 * pulse loop side-effects flow through callbacks rather than zustand
 * directly so the class stays test-friendly and decoupled from store
 * shape — `HudMicButton` wires the callbacks to the conversation store.
 */

import { AudioCapture } from './audio';
import { Vad } from './vad';
import { PartialRecognizer } from './speech-recognition';

const PRE_SPEECH_FRAMES = 5; // 5 * 100 ms frames from audio.ts

export type SttStreamState = 'idle' | 'listening' | 'speaking';

export interface SttStreamCallbacks {
  onState: (state: SttStreamState) => void;
  onLevel: (rms: number) => void;
  onError: (message: string) => void;
  /** Web Speech API interim transcript for grey-text preview. Empty
   * string on speech-end / mic-off. Optional — backend STT path is
   * unaffected when omitted (the canonical transcript still arrives via
   * `voice_final`). */
  onPartial?: (text: string) => void;
  /** Legacy browser-partial fast path. Kept in the callback shape for
   * compatibility, but production speech-end now always commits through
   * backend STT so local Whisper is authoritative. */
  onPartialCommit?: (text: string, mode: 'chat' | 'transcribe') => void;
}

export interface SttStreamOptions {
  sendBinary: (buffer: ArrayBufferLike) => void;
  sendMessage: (type: string, data: Record<string, unknown>) => void;
  /** Phase 16 S3 — barge-in. Called on VAD speech-start; returns `true`
   * when the call cancelled an active TTS playback so the stream can
   * also nudge the server (`voice_cancel`) to drop any in-flight TTS
   * task on its end. Test-friendly: pass a stub in unit tests. */
  onBargein?: () => boolean;
  /** Read at speech-end so each `voice_commit` carries the STT routing
   * mode the operator chose at the moment the utterance closed. Returns
   * `'chat'` (dispatch through chat_brain) or `'transcribe'` (route the
   * transcript into the chat input). Defaults to `'chat'` when omitted.
   * `HudMicButton` derives this from `voiceMode` ('speak' → 'chat',
   * 'transcribe' → 'transcribe'). */
  getMicMode?: () => 'chat' | 'transcribe';
}

export class SttStream {
  private capture = new AudioCapture();
  private vad = new Vad();
  private speaking = false;
  private state: SttStreamState = 'idle';
  private partial: PartialRecognizer | null = null;
  private preSpeechFrames: Int16Array[] = [];

  constructor(
    private opts: SttStreamOptions,
    private cbs: SttStreamCallbacks,
  ) {
    if (cbs.onPartial) {
      this.partial = new PartialRecognizer({
        onPartial: (text) => {
          cbs.onPartial?.(text);
        },
      });
    }
  }

  async start(): Promise<void> {
    if (this.state !== 'idle') return;
    try {
      const stream = await this.capture.start({
        onFrame: (pcm) => {
          if (this.speaking) {
            this.opts.sendBinary(pcm.buffer as ArrayBuffer);
            return;
          }
          this.preSpeechFrames.push(new Int16Array(pcm));
          while (this.preSpeechFrames.length > PRE_SPEECH_FRAMES) {
            this.preSpeechFrames.shift();
          }
        },
        onLevel: (rms) => this.cbs.onLevel(rms),
      });
      this.setState('listening');
      await this.vad.start(
        { getStream: () => stream },
        {
          onSpeechStart: () => {
            // Barge-in: cancel local TTS playback and tell the server
            // to drop the in-flight chat_brain turn + TTS chain. The
            // ledger has already debited synthesised audio; cancelling
            // only stops playback / future synthesis, not billing.
            if (this.opts.onBargein?.()) {
              this.opts.sendMessage('voice_cancel', { reason: 'barge_in' });
            }
            this.speaking = true;
            for (const frame of this.preSpeechFrames) {
              this.opts.sendBinary(frame.buffer as ArrayBuffer);
            }
            this.preSpeechFrames = [];
            this.setState('speaking');
            // Browser-local partial recognizer for grey-text preview.
            // No-op when callbacks didn't register one or the host
            // browser lacks Web Speech API.
            this.partial?.start();
          },
          onSpeechEnd: (_audio: Float32Array) => {
            if (!this.speaking) return;
            this.speaking = false;
            const mode = this.opts.getMicMode?.() ?? 'chat';
            this.opts.sendMessage('voice_commit', { mode });
            // Local state returns to listening so a follow-up utterance
            // is captured without the operator re-clicking the mic. If
            // backend STT is still running, backend voice_state envelopes
            // may temporarily override this with `transcribing`.
            this.setState('listening');
            this.partial?.stop();
            // Clear the preview the moment speech ends; backend STT will
            // emit the authoritative `voice_final` later.
            this.cbs.onPartial?.('');
          },
        },
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      console.error('[voice] SttStream.start failed:', err);
      this.cbs.onError(`mic: ${msg}`);
      await this.stop();
    }
  }

  async stop(): Promise<void> {
    if (this.state === 'idle') return;
    if (this.speaking) {
      // `voice_cancel` clears the chunk-commit buffer + any pending
      // TTS. Same envelope works for both transcribe and speak modes.
      this.opts.sendMessage('voice_cancel', {});
      this.speaking = false;
    }
    this.preSpeechFrames = [];
    await this.vad.stop();
    this.capture.stop();
    this.partial?.stop();
    this.cbs.onPartial?.('');
    this.setState('idle');
    this.cbs.onLevel(0);
  }

  async toggle(): Promise<void> {
    if (this.state === 'idle') {
      await this.start();
    } else {
      await this.stop();
    }
  }

  get currentState(): SttStreamState {
    return this.state;
  }

  private setState(next: SttStreamState): void {
    if (this.state === next) return;
    this.state = next;
    this.cbs.onState(next);
  }
}

let _singleton: SttStream | null = null;

/** Module-level singleton — `HudMicButton` constructs / shares one. */
export function getSttStream(
  opts: SttStreamOptions,
  cbs: SttStreamCallbacks,
): SttStream {
  if (!_singleton) {
    _singleton = new SttStream(opts, cbs);
  }
  return _singleton;
}

export function resetSttStream(): void {
  _singleton = null;
}
