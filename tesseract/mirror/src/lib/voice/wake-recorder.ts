/**
 * Recording takes for the wake-word check.
 *
 * A take is **one utterance, trimmed by voice activity detection** — not a
 * fixed window. The first version recorded a fixed 2.5 s per take, which
 * meant a two-word phrase arrived followed by a second of room tone. That
 * padding is what destroyed the embedding matcher this feature used to run
 * on, and while the keyword decoder is not vulnerable the same way, a take
 * that is mostly silence still says less about the phrase than one that is
 * not.
 *
 * Browser noise suppression is already on (`audio.ts` requests
 * `noiseSuppression`, `echoCancellation` and `autoGainControl`) and does not
 * substitute for this. Not recording the silence in the first place is also
 * how every system that ships an enrollment step does it.
 *
 * `Vad` and `AudioCapture` share one `getUserMedia` stream, the same way the
 * live mic path uses them, so this opens no second microphone.
 */

import { AudioCapture } from "./audio";
import { Vad } from "./vad";

/** 16 kHz, 16-bit, mono — what the decoder reads, and what the VAD hands
 * back. */
const SAMPLE_RATE = 16_000;

/** Longest a take will wait for the operator to say something and stop. Not
 * a recording length: the utterance itself is however long it is. This only
 * stops a take hanging forever when nobody speaks, so the guided run can say
 * so rather than sitting there. */
export const TAKE_TIMEOUT_MS = 12_000;

/** Under this a take is too short to carry a two-word phrase at all and
 * would come back undecidable. Caught here where it can be re-taken, rather
 * than at the verdict where it reads as a failure. */
export const MIN_TAKE_MS = 800;

export interface Take {
  /** base64 of raw int16 LE mono PCM — what the endpoint decodes. */
  audio_b64: string;
  seconds: number;
}

function toBase64(samples: Float32Array): string {
  const pcm = new Int16Array(samples.length);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  const bytes = new Uint8Array(pcm.buffer);
  // Chunked: String.fromCharCode(...bytes) over a few hundred KB overflows
  // the argument limit and throws, which would surface as a failed recording
  // rather than as the stack overflow it is.
  let binary = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

export class WakeRecorder {
  private capture = new AudioCapture();
  private vad = new Vad();
  private active = false;

  /** Wait for one utterance and return it, trimmed to the speech.
   *
   * `onLevel` drives the meter so the operator can see the microphone is
   * hearing them; `onSpeaking` fires when the VAD decides they have started,
   * which is what lets the screen stop saying "waiting".
   */
  async record(
    onLevel?: (rms: number) => void,
    onSpeaking?: () => void,
  ): Promise<Take> {
    if (this.active) throw new Error("already recording");
    this.active = true;

    let settle: (v: Float32Array | null) => void = () => {};
    const utterance = new Promise<Float32Array | null>((r) => (settle = r));
    let timer = 0;

    try {
      const stream = await this.capture.start({
        // The frames go nowhere: `AudioCapture` is started because the VAD
        // reads the same MediaStream, and because it is what produces the
        // level meter.
        onFrame: () => {},
        onLevel: (rms) => onLevel?.(rms),
      });
      await this.vad.start(
        { getStream: () => stream },
        {
          onSpeechStart: () => onSpeaking?.(),
          onSpeechEnd: (audio) => settle(audio),
        },
      );
      timer = window.setTimeout(() => settle(null), TAKE_TIMEOUT_MS);

      const audio = await utterance;
      if (audio === null) {
        throw new Error("nothing heard — check the microphone and try again");
      }
      const seconds = audio.length / SAMPLE_RATE;
      if (seconds * 1000 < MIN_TAKE_MS) {
        throw new Error(`that take was ${seconds.toFixed(1)}s — say it again`);
      }
      return { audio_b64: toBase64(audio), seconds };
    } finally {
      window.clearTimeout(timer);
      await this.teardown();
      onLevel?.(0);
    }
  }

  private async teardown(): Promise<void> {
    this.active = false;
    await this.vad.stop();
    this.capture.stop();
  }

  /** Abandon the current take. The pending `record()` rejects through its own
   * timeout path once the microphone is gone. */
  stop(): void {
    void this.teardown();
  }

  /** Release the microphone — called on unmount, so leaving the screen
   * mid-take does not leave capture running behind it. */
  dispose(): void {
    void this.teardown();
  }
}

export function totalSeconds(takes: Take[]): number {
  return takes.reduce((n, t) => n + t.seconds, 0);
}
