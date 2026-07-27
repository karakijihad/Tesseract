import { useEntityStore } from "../entity";
import { useVoiceStore } from "../voice";
import { getTtsPlayer } from "../../lib/voice/tts-player";
import { setOrbState } from "./orb";

// One-time wiring of the singleton TTS player → voice/entity stores.
// `getTtsPlayer` captures opts on first call; we lazy-init here so the
// dispatch module is the canonical seed point. Subsequent callers
// (`stt-stream.ts` barge-in path) reuse the same instance.
let _ttsWired = false;
export function ensureTtsPlayer() {
  if (_ttsWired) return getTtsPlayer();
  _ttsWired = true;
  return getTtsPlayer({
    onStateChange: (speaking) => {
      const voice = useVoiceStore.getState();
      const entity = useEntityStore.getState();
      if (speaking) {
        voice.setState("speaking_back");
        setOrbState("speaking");
        return;
      }
      // TTS ended. Always clear the orb's `speaking` state — in Speak
      // mode the mic stays hot during playback so VAD can overwrite
      // `voice.state` from `speaking_back` to `listening`/`speaking_in`
      // before the TTS-end fires; gating on `voice.state === 'speaking_back'`
      // would leave the orb stuck on `speaking`. The next VAD transition
      // (from HudMicButton's `onState` callback) drives the orb back
      // to `listening` once the operator's speech-start fires.
      if (entity.state === "speaking") setOrbState("idle");
      if (voice.state === "speaking_back") voice.setState("idle");
    },
    onError: (msg) => useVoiceStore.getState().setError(msg),
  });
}
