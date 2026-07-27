/**
 * VAD wrapper around `@ricky0123/vad-web` (Silero VAD WASM).
 *
 * Phase 16 S2: client-side VAD detects end-of-speech locally so we can
 * commit the buffered PCM to the server without round-tripping every
 * frame through the wire while we wait for silence. The package owns
 * its own AudioContext + worklet under the hood; we hand it a
 * `getStream` thunk that returns the `AudioCapture`-managed
 * `MediaStream` so both pipelines share one mic acquisition.
 *
 * The package is loaded lazily via dynamic import on first `start()`
 * — its WASM bundle is ~2.5 MB and shouldn't tax the initial page
 * weight when voice is unused.
 *
 * Asset paths are pinned to `/vad/` (vendored under `public/vad/` —
 * see Vite static-serve). Defaulting on the package's CDN-derived path
 * resolution would break offline / Tauri builds.
 */

const VAD_ASSET_BASE = '/vad/';

export interface VadCallbacks {
  onSpeechStart?: () => void;
  /** Float32Array audio segment that the VAD already isolated as
   *  speech (16 kHz mono). S2 ignores this; S3 may consume it for
   *  client-side waveform-of-just-speech or barge-in heuristics. */
  onSpeechEnd?: (audio: Float32Array) => void;
}

export interface VadOptions {
  getStream: () => Promise<MediaStream> | MediaStream;
}

interface MicVadInstance {
  start: () => Promise<void>;
  destroy: () => Promise<void>;
}

export class Vad {
  private mic: MicVadInstance | null = null;

  async start(opts: VadOptions, callbacks: VadCallbacks): Promise<void> {
    if (this.mic) return;
    const mod = await import('@ricky0123/vad-web');
    this.mic = await mod.MicVAD.new({
      // Pin asset paths to the vendored copy under public/vad/.
      // Fixes offline + Tauri + production builds where the package's
      // default `currentScript.src` resolution would point nowhere.
      baseAssetPath: VAD_ASSET_BASE,
      onnxWASMBasePath: VAD_ASSET_BASE,
      // Use the v5 Silero model — it's what `copy-vad-assets.mjs`
      // ships into public/vad/. The package's default is "legacy",
      // which references silero_vad_legacy.onnx (not copied).
      model: 'v5',
      // Tighten thresholds vs Silero defaults so brief noise bursts
      // (keyboard clicks, coughs, breath, TTS bleed via imperfect
      // browser AEC) don't fire false `onSpeechStart`. 280 ms requires
      // a sustained vocal segment — a single inhale, click, or echo
      // blip can't satisfy it, but normal speech onset clears it
      // comfortably (a typical word is 200-400 ms).
      positiveSpeechThreshold: 0.55,
      redemptionMs: 2500,
      minSpeechMs: 500,
      // The package's `getStream` runs ONCE at construction; we resolve
      // it eagerly so the same MediaStream powers both pipelines.
      getStream: async () => Promise.resolve(opts.getStream()),
      onSpeechStart: () => {
        callbacks.onSpeechStart?.();
      },
      onSpeechEnd: (audio: Float32Array) => {
        // The audio payload is the VAD-isolated speech segment. S2
        // ignores it (server already buffered the same frames). S3
        // may use it for client-side echo-cancel hints.
        callbacks.onSpeechEnd?.(audio);
      },
    });
    await this.mic.start();
  }

  async stop(): Promise<void> {
    if (!this.mic) return;
    try {
      await this.mic.destroy();
    } catch {
      // Destroy failures are non-fatal; the next `start` constructs anew.
    }
    this.mic = null;
  }

  get isActive(): boolean {
    return this.mic !== null;
  }
}
