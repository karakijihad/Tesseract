import type {
  Envelope,
  TtsChunkData,
  VoiceFinalData,
  VoiceInstructionData,
  VoiceStateData,
} from "../../lib/types";
import { useConversationStore } from "../conversation";
import { useTerminalStore } from "../terminal";
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
      // The server's decision wins, and is the ONLY input to this branch.
      // It resolved the routing before transcribing and has already acted
      // on it (dispatched a turn, or not); honouring a mode the operator
      // flipped to mid-utterance would make the two halves disagree about
      // where the words went. Re-deriving it from `voiceMode` here would
      // also be a second copy of `voice_modes.py::_DESTINATION_BY_MODE` —
      // the duplication this field exists to remove.
      // An unrecognised (or absent) value resolves to `input`, matching
      // `voice_modes.py::normalize_voice_mode`: handing the operator their
      // own words back is the only outcome that is wrong in no dangerous
      // way. Falling through to the chat branch would post a bubble for a
      // turn the server may never have dispatched.
      const destination =
        data.destination === "terminal" || data.destination === "chat"
          ? data.destination
          : "input";
      if (destination === "terminal") {
        // AS-2 — the transcript is typed into the focused pane and never
        // dispatched. The backend already resolved terminal mode to its
        // transcribe contract (no turn, no TTS); this only picks the
        // destination.
        const result = useTerminalStore.getState().typeIntoFocusedPane(text);
        if (!result.typed) {
          useToastStore
            .getState()
            .push("Terminal mode: no live terminal pane to type into", "warning");
        } else if (result.sanitized) {
          // The line in the pane is not what they said. Silently rewriting
          // speech that is about to become a command is the one place this
          // mode cannot afford to be quiet.
          useToastStore
            .getState()
            .push("Dictation contained line breaks — typed as a single line", "warning");
        }
        break;
      }
      if (destination === "input") {
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
    case "voice_discarded": {
      // The wake-word gate refused this utterance. It arrives instead of
      // `voice_final`, so there is deliberately no chat bubble and no
      // toast — a gate that nagged on every passing conversation would
      // be worse than no gate. The pulse row (pushed for every envelope
      // upstream in `handleEnvelope`) is where it is observable.
      useVoiceStore.getState().setState("idle");
      break;
    }
    case "tts_chunk": {
      if (useConversationStore.getState().dropTtsUntilTurnEnd) {
        break;
      }
      // audio is per-chat (chat_id rides the envelope);
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
      // TTS has no cloud lane to fall back to, so when synthesis is
      // gated or down the operator gets the reason instead of silence.
      if (data.instruction) {
        useToastStore.getState().push(data.instruction, "warning");
      }
      break;
    }
    default:
      console.debug("[dispatch] unhandled voice type:", env.type);
  }
}
