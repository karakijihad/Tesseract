import { useVoiceStore } from '../../stores/voice';

/**
 * VoiceStatus — Phase 16 visual compensation row above ChatInput.
 *
 * Renders nothing in `idle`. In `listening` / `speaking_in`, draws an
 * 8-bar waveform driven by `audioLevel` and (when available) the
 * browser-local interim transcript as grey-italic preview text. In
 * `transcribing`, shows an animated `…` placeholder so the operator
 * sees that the utterance was heard and is being processed (the gap
 * between speech-end and `voice_final` is typically 150–300 ms but
 * feels longer without a cue).
 *
 * The partial preview comes from the Web Speech API (frontend-only) —
 * Gemini Flash audio on the backend yields one final, no streaming
 * partials. When the host browser lacks the API, the waveform alone
 * stands in for "the assistant hears you" feedback.
 */
const BAR_COUNT = 8;

export function VoiceStatus() {
  const state = useVoiceStore((s) => s.state);
  const level = useVoiceStore((s) => s.audioLevel);

  if (state === 'idle' || state === 'speaking_back') return null;

  if (state === 'transcribing') {
    return (
      <div className="voice-status voice-status--transcribing" role="status" aria-live="polite">
        <span className="voice-status__label">transcribing</span>
        <span className="voice-status__dots" aria-hidden="true">
          <span /> <span /> <span />
        </span>
      </div>
    );
  }

  // listening / speaking_in: waveform only. The live transcript preview
  // stays in the input field so the conversation list remains stable.
  const intensity = Math.min(1, level * 6);
  return (
    <div
      className={`voice-status voice-status--waveform${
        state === 'speaking_in' ? ' is-speaking' : ''
      }`}
      role="status"
      aria-live="off"
    >
      <span className="voice-status__label t-meta">
        {state === 'speaking_in' ? 'hearing you' : 'listening'}
      </span>
      <span className="voice-waveform" aria-hidden="true">
        {Array.from({ length: BAR_COUNT }, (_, i) => {
          const phase = (i / (BAR_COUNT - 1)) * Math.PI;
          const h = 0.3 + 0.7 * Math.sin(phase) * intensity;
          return <span key={i} style={{ ['--h' as string]: h.toFixed(3) }} />;
        })}
      </span>
    </div>
  );
}
