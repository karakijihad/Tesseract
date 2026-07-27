/**
 * TtsPlayer — decode + queue + play `tts_chunk` audio blobs.
 *
 * One `AudioContext` per page. Each `tts_chunk` envelope arrives with
 * base64-encoded WAV bytes (24 kHz / 16-bit / mono PCM wrapped at the
 * Gemini Flash TTS provider). We `decodeAudioData` and schedule the
 * resulting buffer on a single linear timeline so consecutive chunks
 * play gap-free.
 *
 * `cancel()` stops everything currently scheduled and clears the queue
 * — this is the barge-in path. Active source nodes get `.stop()`; the
 * `nextStartTime` cursor resets so the next chunk plays immediately.
 *
 * `isSpeaking` is true while ≥1 source node is queued. Subscribers
 * (entity store, voice store) read it through `onStateChange`.
 */

export interface TtsPlayerOptions {
  onStateChange?: (speaking: boolean) => void;
  onError?: (message: string) => void;
}

function base64ToArrayBuffer(b64: string): ArrayBuffer {
  const binary = atob(b64);
  const len = binary.length;
  const bytes = new Uint8Array(len);
  for (let i = 0; i < len; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

export class TtsPlayer {
  private ctx: AudioContext | null = null;
  private active = new Set<AudioBufferSourceNode>();
  private nextStartTime = 0;
  private speaking = false;

  constructor(private opts: TtsPlayerOptions = {}) {}

  configure(opts: TtsPlayerOptions): void {
    this.opts = { ...this.opts, ...opts };
  }

  /** Prime browser playback from an operator gesture. Chrome may create
   * AudioContext in `suspended` state when the eventual `tts_chunk` arrives
   * from a websocket callback; resuming here keeps Speak mode audible. */
  async arm(): Promise<void> {
    try {
      await this._ensureRunningContext();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      this.opts.onError?.(`tts audio: ${msg}`);
    }
  }

  /** Decode `chunk.audio_b64` and schedule it on the playback timeline.
   * Empty audio is a no-op; only `is_final` will fire a state-change. */
  async play(chunk: { audio_b64: string; is_final: boolean }): Promise<void> {
    if (chunk.audio_b64) {
      try {
        const ctx = await this._ensureRunningContext();
        const buf = await ctx.decodeAudioData(base64ToArrayBuffer(chunk.audio_b64));
        this._enqueueBuffer(ctx, buf);
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        this.opts.onError?.(`tts decode: ${msg}`);
      }
    }
    if (chunk.is_final && !this.active.size) {
      // No audio queued and the turn is closed — flip back to idle.
      this._setSpeaking(false);
    }
  }

  /** Stop every queued source and clear the timeline. Idempotent. */
  cancel(): void {
    if (!this.ctx && !this.active.size) return;
    for (const node of this.active) {
      try { node.stop(); } catch { /* already stopped */ }
      try { node.disconnect(); } catch { /* not connected */ }
    }
    this.active.clear();
    this.nextStartTime = 0;
    this._setSpeaking(false);
  }

  get isSpeaking(): boolean {
    return this.speaking;
  }

  private _ensureContext(): AudioContext {
    if (this.ctx && this.ctx.state !== 'closed') return this.ctx;
    const Ctor: typeof AudioContext =
      (window as unknown as { AudioContext: typeof AudioContext; webkitAudioContext?: typeof AudioContext }).AudioContext ||
      (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    this.ctx = new Ctor();
    this.nextStartTime = 0;
    return this.ctx;
  }

  private async _ensureRunningContext(): Promise<AudioContext> {
    const ctx = this._ensureContext();
    if (ctx.state === 'suspended') {
      await ctx.resume();
    }
    return ctx;
  }

  private _enqueueBuffer(ctx: AudioContext, buf: AudioBuffer): void {
    const source = ctx.createBufferSource();
    source.buffer = buf;
    source.connect(ctx.destination);

    const now = ctx.currentTime;
    const startAt = Math.max(this.nextStartTime, now);
    source.start(startAt);
    this.nextStartTime = startAt + buf.duration;

    this.active.add(source);
    this._setSpeaking(true);

    source.onended = () => {
      this.active.delete(source);
      try { source.disconnect(); } catch { /* not connected */ }
      if (this.active.size === 0) {
        // Fully drained — reset cursor so the next turn schedules
        // against `currentTime` cleanly even after a long quiet gap.
        this.nextStartTime = 0;
        this._setSpeaking(false);
      }
    };
  }

  private _setSpeaking(next: boolean): void {
    if (this.speaking === next) return;
    this.speaking = next;
    this.opts.onStateChange?.(next);
  }
}

let _singleton: TtsPlayer | null = null;

export function getTtsPlayer(opts?: TtsPlayerOptions): TtsPlayer {
  if (!_singleton) _singleton = new TtsPlayer(opts);
  else if (opts) _singleton.configure(opts);
  return _singleton;
}

export function resetTtsPlayer(): void {
  _singleton?.cancel();
  _singleton = null;
}
