/**
 * AudioCapture — getUserMedia + AudioWorklet pipeline that emits 16 kHz
 * mono Int16 PCM frames at ~100 ms cadence and a smoothed RMS audio level
 * for the ChatInput waveform.
 *
 * The captured stream is ALSO handed to `vad.ts` so VAD runs on the same
 * `MediaStream` (one getUserMedia call per session, not two). The VAD
 * package internally connects another AudioContext to the stream — that's
 * fine; both paths read off the same browser source.
 *
 * Frame format on the wire:
 *   - 16 kHz sample rate
 *   - mono
 *   - 16-bit signed little-endian (the server wraps these in a WAV
 *     envelope before forwarding to Gemini Flash audio for transcription)
 */

const TARGET_SAMPLE_RATE = 16_000;
const FRAME_DURATION_MS = 100;
const SAMPLES_PER_FRAME = (TARGET_SAMPLE_RATE * FRAME_DURATION_MS) / 1000; // 1600
const LEVEL_SMOOTHING = 0.6;  // 0..1; higher = sluggier meter

export type FrameCallback = (pcm: Int16Array) => void;
export type LevelCallback = (rms: number) => void;

export interface AudioCaptureOptions {
  onFrame: FrameCallback;
  onLevel?: LevelCallback;
}

const WORKLET_SOURCE = `
class AgentCaptureProcessor extends AudioWorkletProcessor {
  constructor(opts) {
    super();
    this.targetRate = opts.processorOptions.targetRate;
    this.framesPerOutput = opts.processorOptions.framesPerOutput;
    this.ratio = sampleRate / this.targetRate;
    this._buf = [];
    this._readIdx = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    // Naive linear-resample to targetRate. AudioWorklet runs at the
    // device sample rate (commonly 48 kHz); we step by \`ratio\` and
    // pick the nearest sample. Good enough for speech; Gemini Flash
    // audio is forgiving of the artifacts.
    while (this._readIdx < ch.length) {
      const idx = this._readIdx | 0;
      this._buf.push(ch[idx]);
      this._readIdx += this.ratio;
    }
    this._readIdx -= ch.length;
    while (this._buf.length >= this.framesPerOutput) {
      const slice = this._buf.splice(0, this.framesPerOutput);
      const pcm = new Int16Array(slice.length);
      let sumSq = 0;
      for (let i = 0; i < slice.length; i++) {
        const s = Math.max(-1, Math.min(1, slice[i]));
        pcm[i] = (s * 0x7fff) | 0;
        sumSq += s * s;
      }
      const rms = Math.sqrt(sumSq / slice.length);
      this.port.postMessage({ pcm: pcm.buffer, rms }, [pcm.buffer]);
    }
    return true;
  }
}
registerProcessor('agent-capture', AgentCaptureProcessor);
`;

export class AudioCapture {
  private stream: MediaStream | null = null;
  private ctx: AudioContext | null = null;
  private worklet: AudioWorkletNode | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private smoothedLevel = 0;

  async start(opts: AudioCaptureOptions): Promise<MediaStream> {
    if (this.stream) {
      throw new Error('AudioCapture: already started');
    }
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });

    this.ctx = new AudioContext();
    // Chrome creates the context in `suspended` state when constructed
    // after an `await` (the click gesture context is lost on the
    // microtask boundary). Without resume(), the AudioWorklet's
    // process() never fires — no PCM frames, no level meter.
    if (this.ctx.state === 'suspended') {
      await this.ctx.resume();
    }
    const workletUrl = URL.createObjectURL(
      new Blob([WORKLET_SOURCE], { type: 'application/javascript' }),
    );
    try {
      await this.ctx.audioWorklet.addModule(workletUrl);
    } finally {
      URL.revokeObjectURL(workletUrl);
    }

    this.worklet = new AudioWorkletNode(this.ctx, 'agent-capture', {
      processorOptions: {
        targetRate: TARGET_SAMPLE_RATE,
        framesPerOutput: SAMPLES_PER_FRAME,
      },
    });

    this.worklet.port.onmessage = (ev: MessageEvent) => {
      const { pcm, rms } = ev.data as { pcm: ArrayBuffer; rms: number };
      opts.onFrame(new Int16Array(pcm));
      if (opts.onLevel) {
        this.smoothedLevel =
          LEVEL_SMOOTHING * this.smoothedLevel + (1 - LEVEL_SMOOTHING) * rms;
        opts.onLevel(this.smoothedLevel);
      }
    };

    this.source = this.ctx.createMediaStreamSource(this.stream);
    this.source.connect(this.worklet);
    // The worklet is a dead-end node; we don't connect to destination
    // because we don't want to play the mic back through the speakers.
    return this.stream;
  }

  stop(): void {
    this.worklet?.disconnect();
    this.source?.disconnect();
    this.worklet = null;
    this.source = null;
    if (this.stream) {
      for (const track of this.stream.getTracks()) track.stop();
      this.stream = null;
    }
    if (this.ctx && this.ctx.state !== 'closed') {
      void this.ctx.close();
    }
    this.ctx = null;
    this.smoothedLevel = 0;
  }

  get isActive(): boolean {
    return this.stream !== null;
  }

  get mediaStream(): MediaStream | null {
    return this.stream;
  }
}
