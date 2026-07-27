import { useEffect, useRef, useState } from 'react';
import { postOperatorPost } from '../../lib/api';

type SpeechRecognitionLike = {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  start: () => void;
  stop: () => void;
  onresult: ((event: { results: ArrayLike<{ 0: { transcript: string }; isFinal: boolean }> }) => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  onend: (() => void) | null;
};

type SpeechRecognitionCtor = new () => SpeechRecognitionLike;

function getSpeechRecognitionCtor(): SpeechRecognitionCtor | null {
  if (typeof window === 'undefined') return null;
  const w = window as unknown as {
    SpeechRecognition?: SpeechRecognitionCtor;
    webkitSpeechRecognition?: SpeechRecognitionCtor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export function VoiceComposer() {
  // Resolve support at mount, not at module-load: protects against SSR
  // / jsdom imports where `window` exists but the recognizer doesn't,
  // and lets the value re-evaluate per component instance.
  const [supported, setSupported] = useState(false);
  useEffect(() => {
    setSupported(getSpeechRecognitionCtor() !== null);
  }, []);
  const [listening, setListening] = useState(false);
  const [transcript, setTranscript] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const recogRef = useRef<SpeechRecognitionLike | null>(null);

  useEffect(() => () => {
    try { recogRef.current?.stop(); } catch { /* ignore */ }
  }, []);

  if (!supported) {
    return (
      <button
        type="button"
        className="workspace-voice-trigger is-disabled"
        disabled
        title="Voice composer unavailable — your browser doesn't expose SpeechRecognition"
      >
        Voice unavailable
      </button>
    );
  }

  const start = () => {
    const Ctor = getSpeechRecognitionCtor();
    if (!Ctor) return;
    setError(null);
    setTranscript('');
    const recog = new Ctor();
    recog.continuous = true;
    recog.interimResults = true;
    recog.lang = 'en-US';
    recog.onresult = (event) => {
      let text = '';
      for (let i = 0; i < event.results.length; i += 1) {
        text += event.results[i][0].transcript;
      }
      setTranscript(text);
    };
    recog.onerror = (e) => {
      setError(`mic error: ${e.error}`);
      setListening(false);
    };
    recog.onend = () => setListening(false);
    recogRef.current = recog;
    try {
      recog.start();
      setListening(true);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const stop = () => {
    try { recogRef.current?.stop(); } catch { /* ignore */ }
    setListening(false);
  };

  const submit = async () => {
    const body = transcript.trim();
    if (!body || busy) return;
    setBusy(true);
    setError(null);
    try {
      await postOperatorPost({ title: '', body, source: 'voice' });
      setTranscript('');
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="workspace-voice">
      <button
        type="button"
        className={`workspace-voice-trigger${listening ? ' is-listening' : ''}`}
        onClick={listening ? stop : start}
        aria-pressed={listening}
      >
        {listening ? 'Stop' : 'Voice'}
      </button>
      {(listening || transcript) && (
        <div className="workspace-voice-panel">
          <textarea
            className="workspace-voice-text"
            value={transcript}
            onChange={(e) => setTranscript(e.target.value)}
            placeholder={listening ? 'Listening…' : 'Edit then send'}
            rows={2}
            disabled={busy}
          />
          <div className="workspace-voice-row">
            {error && <span className="workspace-voice-error t-caption">{error}</span>}
            <button
              type="button"
              className="workspace-voice-send"
              onClick={() => void submit()}
              disabled={busy || !transcript.trim()}
            >
              {busy ? 'Sending…' : 'Post'}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
