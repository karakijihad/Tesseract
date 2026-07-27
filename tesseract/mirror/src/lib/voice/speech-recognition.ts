/**
 * Web Speech API wrapper — provides interim partial transcripts for
 * grey-text preview while the operator speaks.
 *
 * Why frontend-only: Gemini Flash audio (the canonical STT path on the
 * backend) is one-shot and yields a single final pair — no native
 * partials. Adding a streaming partial from the backend would mean
 * either re-calling Gemini every N ms (expensive) or swapping STT
 * providers. Web Speech API runs locally in the browser, costs nothing,
 * and now acts as the primary transcript when available. Backend STT
 * remains as fallback when the browser returns no usable partial.
 *
 * Browser support: Chrome / Edge / Safari (vendor-prefixed). When the
 * API isn't available (Firefox, some Tauri builds), `start()` is a
 * no-op — the operator sees the waveform but no grey text. In that
 * case `SttStream` falls back to backend STT at speech-end.
 */

interface MinimalSpeechRecognition {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  start: () => void;
  stop: () => void;
  abort: () => void;
  onresult: ((ev: { resultIndex: number; results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }> }) => void) | null;
  onerror: ((ev: { error?: string }) => void) | null;
  onend: (() => void) | null;
}

type SpeechRecognitionCtor = new () => MinimalSpeechRecognition;

function getCtor(): SpeechRecognitionCtor | null {
  if (typeof window === 'undefined') return null;
  const w = window as unknown as {
    SpeechRecognition?: SpeechRecognitionCtor;
    webkitSpeechRecognition?: SpeechRecognitionCtor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export interface PartialRecognizerCallbacks {
  /** Fired whenever interim transcript text changes — concatenation of
   * every result so far in the current utterance. The operator sees
   * the running grey-text preview update as they speak. */
  onPartial: (text: string) => void;
}

/** Browser-local speech recognizer used for the grey-text preview and
 * as the preferred transcript source when available. */
export class PartialRecognizer {
  private rec: MinimalSpeechRecognition | null = null;
  private active = false;

  constructor(private cbs: PartialRecognizerCallbacks) {}

  /** True when the host browser exposes the Web Speech API. UI can
   * use this to decide whether to render a fallback hint. */
  static get isSupported(): boolean {
    return getCtor() !== null;
  }

  start(): void {
    if (this.active) return;
    const Ctor = getCtor();
    if (!Ctor) return;
    const rec = new Ctor();
    rec.continuous = true;
    rec.interimResults = true;
    rec.lang = navigator.language || 'en-US';
    rec.onresult = (ev) => {
      // Concatenate every result (interim + final) in the current
      // utterance so the grey text builds up as the operator speaks.
      // We don't separate interim/final: at VAD speech-end the latest
      // visible text is committed directly, avoiding a duplicate cloud
      // STT pass.
      let text = '';
      for (let i = 0; i < ev.results.length; i++) {
        const r = ev.results[i];
        text += r[0].transcript;
      }
      this.cbs.onPartial(text);
    };
    rec.onerror = () => {
      // Permission denied / network errors — silently drop the
      // preview. Backend STT path is unaffected.
      this.active = false;
    };
    rec.onend = () => {
      this.active = false;
    };
    try {
      rec.start();
      this.rec = rec;
      this.active = true;
    } catch {
      // Already-started errors land here on rapid restart; treat as no-op.
    }
  }

  stop(): void {
    if (!this.rec) return;
    // Detach handlers BEFORE stopping — the browser may still fire one
    // last `onresult` after stop() (buffered audio segment). Without
    // this, late events would re-populate the store after the caller
    // had already cleared it.
    this.rec.onresult = null;
    this.rec.onerror = null;
    this.rec.onend = null;
    try {
      this.rec.stop();
    } catch {
      /* ignore */
    }
    this.rec = null;
    this.active = false;
  }
}
