import { useEffect, useRef } from 'react';
import { Hint } from '../../ui/Hint';
import { useEntityStore } from '../../../stores/entity';
import { useIdentityStore } from '../../../stores/identity';
import { useVoiceStore, type VoiceMode } from '../../../stores/voice';
import { useWebSocketStore } from '../../../stores/websocket';
import {
  getSttStream,
  resetSttStream,
  type SttStream,
} from '../../../lib/voice/stt-stream';
import { getTtsPlayer } from '../../../lib/voice/tts-player';

// Pipeline-stage labels — what the backend / TTS player is doing
// right now. Distinct from the operator's mic-on/off intent: the mic
// stays hot regardless of which stage the assistant is in.
const STAGE_LABEL: Record<string, string> = {
  idle: 'Idle',
  listening: 'Listening',
  speaking_in: 'Hearing you',
  transcribing: 'Transcribing…',
  speaking_back: 'Replying',
};

export const MODE_PILL: Record<VoiceMode, string> = {
  transcribe: 'Transcribe',
  command: 'Command',
  speak: 'Speak',
  terminal: 'Terminal',
};

/** Hints name the entity rather than hardcoding one — AS-4 makes the name
 * operator-settable, and a caption that still said "the assistant" after a rename
 * would be the most visible place the rename didn't take. */
export function modeHint(mode: VoiceMode, name: string): string {
  const who = name || 'the assistant';
  switch (mode) {
    case 'transcribe':
      return `Transcribe — speech fills the chat input. ${who} stays silent.`;
    case 'command':
      return `Command — speech goes to ${who}. Replies stay text/actions only.`;
    case 'speak':
      return `Speak — speech goes to ${who}. ${who} replies in voice.`;
    case 'terminal':
      return `Terminal — speech is typed into the focused terminal pane. ${who} neither answers nor speaks.`;
  }
}

/** Map `voiceMode` → STT routing for `voice_commit`. `transcribe` and
 * `terminal` both keep the transcript out of chat_brain — the backend
 * resolves them to the same contract and only the destination differs;
 * `command` and `speak` dispatch (the difference is whether the reply is
 * spoken). */
function micRoutingFor(mode: VoiceMode): 'chat' | 'transcribe' {
  return mode === 'transcribe' || mode === 'terminal' ? 'transcribe' : 'chat';
}

/** Cycle: `transcribe → command → speak → terminal → transcribe`. Default
 * is `transcribe` (silent) so a fresh session never speaks unsolicited;
 * `command` adds STT-to-the assistant without TTS reply; `speak` is full STT+TTS;
 * `terminal` routes speech to the PTY instead of the brain. */
export function nextMode(mode: VoiceMode): VoiceMode {
  if (mode === 'transcribe') return 'command';
  if (mode === 'command') return 'speak';
  if (mode === 'speak') return 'terminal';
  return 'transcribe';
}

export function HudMicButton() {
  const state = useVoiceStore((s) => s.state);
  const micActive = useVoiceStore((s) => s.micActive);
  const audioLevel = useVoiceStore((s) => s.audioLevel);
  const voiceMode = useVoiceStore((s) => s.voiceMode);
  const entityName = useIdentityStore((s) => s.name);
  const setState = useVoiceStore((s) => s.setState);
  const setMicActive = useVoiceStore((s) => s.setMicActive);
  const setAudioLevel = useVoiceStore((s) => s.setAudioLevel);
  const setError = useVoiceStore((s) => s.setError);
  const setVoiceMode = useVoiceStore((s) => s.setVoiceMode);
  const setPartialTranscript = useVoiceStore((s) => s.setPartialTranscript);

  const streamRef = useRef<SttStream | null>(null);

  // Sync voice_mode → backend on mount, on every mode change, and on
  // WS (re)connect so the server gates TTS synthesis (transcribe →
  // silent) even after a transport drop.
  const wsStatus = useWebSocketStore((s) => s.status);
  useEffect(() => {
    if (wsStatus !== 'connected') return;
    useWebSocketStore.getState().sendMessage('voice_mode_set', { mode: voiceMode });
  }, [voiceMode, wsStatus]);

  useEffect(() => {
    const ws = useWebSocketStore.getState();
    streamRef.current = getSttStream(
      {
        sendBinary: (buf) => ws.sendBinary(buf),
        sendMessage: (type, data) => ws.sendMessage(type, data),
        onBargein: () => {
          // Audit-3 #4 — speech-start IS the interrupt. Cancel local
          // TTS playback (so the operator stops hearing the old reply)
          // AND tell the backend to drop the in-flight chat_brain turn
          // + TTS chain by returning true (SttStream then sends
          // `voice_cancel reason='barge_in'`). Matches OpenClaw Talk
          // mode: when the operator speaks, the assistant shuts up *and* stops
          // thinking. Compute is no longer wasted on a turn the
          // operator already overrode.
          //
          // AEC-bleed false-trigger: the explicit mic-press gate
          // (`micActive`) is the primary mitigation — VAD only runs
          // while the mic is hot. If TV/leakage triggers prove painful
          // in practice, the next step is wake-word gating via
          // Picovoice Porcupine (small dep, no model training); see
          // audit-3 finding #6 deferral.
          getTtsPlayer().cancel();
          return true;
        },
        getMicMode: () => micRoutingFor(useVoiceStore.getState().voiceMode),
      },
      {
        onState: (next) => {
          // Map SttStream local state into the UI state union AND nudge
          // the orb's body — `listening` is already a registered
          // EntityState (lib/entity/states.ts), so the orb modulates
          // for free.
          const entity = useEntityStore.getState();
          if (next === 'idle') {
            setState('idle');
            // Sync the privacy-mic flag with the actual capture
            // lifecycle. The operator-click path sets `micActive`
            // synchronously; this branch covers internal-stop paths
            // (mic error, page unload, AudioCapture failure) so the
            // indicator never lies about mic-hot status.
            setMicActive(false);
            // Don't stomp on the assistant's reactive states — `thinking` and
            // `speaking` are owned by the chat loop / TTS player.
            if (entity.state === 'listening') entity.setState('idle');
          } else if (next === 'listening') {
            setState('listening');
            if (entity.state === 'idle') entity.setState('listening');
          } else if (next === 'speaking') {
            setState('speaking_in');
            if (entity.state === 'idle') entity.setState('listening');
          }
        },
        onLevel: (rms) => setAudioLevel(rms),
        onError: (msg) => setError(msg),
        onPartial: (text) => setPartialTranscript(text),
      },
    );
    return () => {
      // Singleton survives across remounts; only reset on full app teardown.
    };
  }, [setState, setMicActive, setAudioLevel, setError, setPartialTranscript]);

  // Mic on/off is tied STRICTLY to operator intent (`micActive`),
  // never to backend pipeline state. Privacy contract: when the
  // operator clicks the mic on, it stays on until they click it off
  // — the assistant replying, transcribing, or going `idle` from a backend
  // signal must not flip the indicator off.
  const isOn = micActive;
  const isHot = micActive && (state === 'listening' || state === 'speaking_in');

  const onClick = async () => {
    const stream = streamRef.current;
    if (!stream) return;
    const intentOn = useVoiceStore.getState().micActive;
    if (!intentOn && voiceMode === 'speak') {
      void getTtsPlayer().arm();
    }
    // `micActive` is the operator-intent gate; flip it before toggling
    // the underlying capture so the indicator lights immediately even
    // if AudioCapture.start() takes a few hundred ms (permission
    // prompt, device select). On stop it flips off in the same beat.
    setMicActive(!intentOn);
    await stream.toggle();
  };

  // Drive the mic-button glow off audioLevel via a CSS custom property.
  // Idle / disabled paths return zero so no transient flicker.
  const micLevel = isHot ? Math.min(1, audioLevel * 4) : 0;

  const onCycleMode = () => {
    const next = nextMode(voiceMode);
    if (next === 'speak') {
      void getTtsPlayer().arm();
    }
    setVoiceMode(next);
  };

  return (
    <div className="hud-mic-group">
      <Hint label={modeHint(voiceMode, entityName)} position="top" maxWidth={260}>
        <button
          type="button"
          className={`hud-voice-mode is-${voiceMode}`}
          onClick={onCycleMode}
          aria-label={`voice mode: ${voiceMode}, click to cycle`}
        >
          {MODE_PILL[voiceMode]}
        </button>
      </Hint>
      <Hint
        label={
          isOn
            ? `Mic ON — ${STAGE_LABEL[state] ?? state} — click to mute`
            : 'Mic OFF — click to capture'
        }
        position="top"
        maxWidth={240}
      >
        <button
          type="button"
          className={`hud-mic${isOn ? ' is-on' : ' is-off'}${
            isOn && state === 'speaking_in' ? ' is-speaking' : ''
          }${isOn && state === 'transcribing' ? ' is-transcribing' : ''}`}
          onClick={onClick}
          aria-label={isOn ? 'turn mic off' : 'turn mic on'}
          aria-pressed={isOn}
          style={{ ['--mic-level' as string]: micLevel.toFixed(3) }}
        >
          <MicGlyph on={isOn} />
        </button>
      </Hint>
    </div>
  );
}

function MicGlyph({ on }: { on: boolean }) {
  // Inline SVG mic. When ON: full mic + tiny status dot. When OFF: a
  // muted mic with a slash through it so the operator reads it as
  // "off" at a glance, even before checking color.
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="6" y="2" width="4" height="7" rx="2" />
      <path d="M3.5 8.5a4.5 4.5 0 0 0 9 0" />
      <path d="M8 13v2" />
      {on ? (
        <circle cx="13" cy="3.5" r="1.4" fill="currentColor" stroke="none" />
      ) : (
        <line x1="2.5" y1="13.5" x2="13.5" y2="2.5" />
      )}
    </svg>
  );
}

// Re-export resetSttStream for tests / hot-reload teardown convenience.
export { resetSttStream };
