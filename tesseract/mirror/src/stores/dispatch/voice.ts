import type {
  Envelope,
  TtsChunkData,
  VoiceFinalData,
  VoiceInstructionData,
  VoiceStateData,
} from "../../lib/types";
import { useConversationStore } from "../conversation";
import { useToastStore } from "../toasts";
import { useVoiceStore } from "../voice";
import { ensureTtsPlayer } from "./tts";

export function handleVoice(env: Envelope): void {
  switch (env.type) {
    case "voice_state": {
      const data = env.data as unknown as VoiceStateData;
      useVoiceStore.getState().applyBackendState(data.state);
      // Backend reaching `idle` means the TTS pipeline is fully drained
      // (post-`stopVoice` teardown completes here). Clear the drop guard
      // so the next assistant turn's tts_chunks aren't silently swallowed.
      if (data.state === "idle") {
        useConversationStore.getState().clearTtsDropFlag();
      }
      break;
    }
    case "voice_final": {
      const data = env.data as unknown as VoiceFinalData;
      const text = (data.text ?? "").trim();
      const voice = useVoiceStore.getState();
      voice.setState("idle");
      if (!text) {
        useToastStore.getState().push("Voice: nothing transcribed", "warning");
        break;
      }
      if (voice.voiceMode === "transcribe") {
        if (voice.suppressNextBackendDictation) {
          voice.setSuppressNextBackendDictation(false);
          break;
        }
        // Mode C — backend skipped chat dispatch + TTS. Park the text
        // for `ChatInput` to pull into its textarea so the operator can
        // review/edit/send.
        voice.setPendingDictation(text);
      } else {
        // Mode B (`chat`): backend already dispatched the turn via
        // `_start_turn`. Append the user-message bubble so the
        // operator sees their spoken text in the chat history (the
        // typed-message path appends optimistically in `sendUserMessage`;
        // voice has to mirror that here). Voice envelopes are session-scoped
        // (no chat_id) → the store routes to the active chat.
        useConversationStore
          .getState()
          .appendUserMessage(env.chat_id ?? null, text);
      }
      break;
    }
    case "tts_chunk": {
      if (useConversationStore.getState().dropTtsUntilTurnEnd) {
        break;
      }
      // parallel-tars P6 — audio is per-chat (chat_id rides the envelope);
      // only the active chat is audible. Chunks already in flight when the
      // operator switched away are dropped here, not played over the new chat.
      const activeId = useConversationStore.getState().activeChatId;
      if (env.chat_id && activeId && env.chat_id !== activeId) {
        break;
      }
      const data = env.data as unknown as TtsChunkData;
      void ensureTtsPlayer().play({
        audio_b64: data.audio_b64,
        is_final: data.is_final,
      });
      break;
    }
    case "voice_instruction": {
      const data = env.data as unknown as VoiceInstructionData;
      useVoiceStore.getState().setInstruction(data);
      // Backend emits an `instruction`-only toast when the daily TTS
      // budget is hit — no local fallback after the G2 cutover, so the
      // operator just sees why TARS went silent.
      if (data.instruction) {
        useToastStore.getState().push(data.instruction, "warning");
      }
      break;
    }
    default:
      console.debug("[dispatch] unhandled voice type:", env.type);
  }
}
