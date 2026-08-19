import type {
  Envelope,
  LoopEndData,
  LoopStartData,
  StreamErrorData,
  StreamStopData,
  StreamTextData,
  StreamToolCallEndData,
  StreamToolResultData,
} from "../../lib/types";
import { getController } from "../../lib/entity/registry";
import { ENTITY_FALLBACK } from "../../hooks/useEntityName";
import { useIdentityStore } from "../identity";
import { useConversationStore } from "../conversation";
import { useEntityStore } from "../entity";
import { usePulseStore } from "../pulse";
import { useSessionStore } from "../session";
import { useTasksStore, type TaskItem } from "../tasks";
import { useToastStore } from "../toasts";
import { useToolActivityStore } from "../toolActivity";
import { setOrbState, TRANSIENT_ERROR_CLEAR_MS } from "./orb";
import {
  addPendingTextChars,
  scheduleSignalsImpulse,
  type Signals,
} from "./signals";
import { ensureTtsPlayer } from "./tts";

export function handleLoop(env: Envelope, signals: Signals | null): void {
  // Synthetic workspace turn envelopes are dropped at the top of
  // `handleEnvelope` via `isSyntheticTurn(env) && category !== 'workspace'`
  // — they never reach this handler. The legacy `_workspaceTurnActive`
  // boolean (pre-WP-2 serial gate) was removed 2026-05-22 after the
  // audit flagged it as broken under WP-2 concurrent turns: a singleton
  // boolean cannot represent N parallel turns. The top guard supersedes.
  //
  // Agent Trace right-panel section was removed (operator: redundant
  // with chat). The `agentSession` store kept getting per-chunk writes
  // that re-rendered every subscriber even though no UI consumed them
  // any more — measurable streaming-glitch contributor. All `agent.*`
  // dispatch calls dropped accordingly.
  const chat = useConversationStore.getState();
  // mirror-multi-chat inc.B — route every turn-scoped mutation to the chat
  // the envelope belongs to (inc.A stamps it); null → active-chat fallback.
  const cid = env.chat_id ?? null;

  switch (env.type) {
    case "loop_start": {
      const data = env.data as unknown as LoopStartData;
      chat.beginTurn(cid, String(data.turn));
      signals?.onStep();
      setOrbState("thinking");
      break;
    }
    case "stream_start": {
      const turnId = (env.data as { turn_id?: unknown })?.turn_id;
      if (typeof turnId === "string" && turnId) {
        usePulseStore.getState().beginTurn(turnId);
      }
      break;
    }
    case "stream_text": {
      const data = env.data as unknown as StreamTextData;
      if (data.kind === "thinking") {
        // Model chain-of-thought (backend `thinking` kind, 2026-07-16). No
        // chat surface yet — the collapsed thinking block is parked — and it
        // must NOT fall through to the answer bubble. Keep the orb impulse
        // so the operator still sees activity while a reasoning model works.
        if (signals !== null) {
          addPendingTextChars(data.delta.length);
          scheduleSignalsImpulse(signals);
        }
        break;
      }
      chat.appendDelta(
        cid,
        data.delta,
        data.kind === "status" || data.kind === "intent"
          ? "intent"
          : data.kind === "spoken"
            ? "spoken"
            : "answer",
      );
      // Coalesce orb-intensity impulses to one per frame — same idea as
      // the chat bubble's rAF flush. Otherwise short backend chunks
      // (1-5 chars) make the orb visibly jitter while the bubble text
      // streams smoothly.
      if (signals !== null) {
        addPendingTextChars(data.delta.length);
        scheduleSignalsImpulse(signals);
      }
      setOrbState("speaking");
      break;
    }
    case "stream_tool_call_start": {
      // agent-sticky states (deep_focus / dreaming / happy) are the operator-
      // visible mood for the whole turn. Tool calls inside that turn must
      // not blink the orb back to `thinking` — that creates the "lasts one
      // tool turn" flicker. `stream_text` still overrides (the assistant speaking
      // takes visual priority); next `loop_start` still resets to thinking.
      const live = useEntityStore.getState().state;
      if (live !== "deep_focus" && live !== "dreaming" && live !== "happy") {
        setOrbState("thinking");
      }
      break;
    }
    case "queued_message": {
      // Phase 2 (revised 2026-05-11): no-op. The operator's own user
      // bubble (added by sendUserMessage) already shows status='queued'
      // — that's the single source of truth. The envelope is still
      // emitted by the backend for log/audit purposes but the
      // frontend no longer renders a duplicate badge.
      break;
    }
    case "steered": {
      // conversation-layer Task 5.2 (Q3 frontend) — confirms a redirect
      // landed in the CURRENT turn. Same no-op precedent as
      // `queued_message` above: `sendSteer` already rendered the
      // operator's bubble (flagged `steered: true`) optimistically at
      // send time, so there is nothing left to apply here. Kept as an
      // explicit case (not falling into `default`) for log/audit parity
      // with the backend's `steered` contract and so a future consumer
      // has an obvious place to hook in.
      //
      // Review fix-pass — `applied: false` means the backend's focused-
      // chat degrade path fired: no turn was active to redirect, so it
      // fell back to a normal `_start_turn` send instead. The bubble
      // `sendSteer` rendered is wrong (permanently flagged "redirected"
      // on what was actually a fresh normal turn) — clear it.
      const data = env.data as { applied?: unknown };
      if (data.applied === false) {
        chat.clearDegradedSteer(cid);
      }
      break;
    }
    case "steer_rejected": {
      // Q3 frontend — a steer targeting a background (non-focused) chat
      // with no active turn was dropped rather than misrouted (house
      // convention: drops are never silent, cf. `chat_queue_overflow`
      // below). Toast-only, mirroring `stream_error`'s `severity:
      // 'warning'` treatment above (operator-input miss, not a system
      // failure) — no chat bubble, no orb flip.
      const data = env.data as { text?: unknown; reason?: unknown };
      const reason =
        typeof data.reason === "string"
          ? data.reason
          : "no active turn for that chat";
      useToastStore.getState().push(`Steer not applied — ${reason}`, "warning");
      break;
    }
    case "chat_queue_overflow": {
      // Q2 backend (Task 4.2) shipped with no frontend handler until this
      // pass. Same drop-is-never-silent treatment as `steer_rejected`
      // above: the arriving message was dropped because the per-chat FIFO
      // queue was already full.
      const data = env.data as { text?: unknown; queue_size?: unknown };
      const size = typeof data.queue_size === "number" ? data.queue_size : "?";
      useToastStore
        .getState()
        .push(`Message dropped — queue full (${size} pending)`, "warning");
      break;
    }
    case "stream_user_inject": {
      // Phase 2 — backend just folded N queued messages into history at
      // a tool boundary. Flip the oldest N queued user bubbles to
      // 'complete' so the dashed-border / queued pill clears.
      const data = env.data as { count?: unknown };
      const count = typeof data.count === "number" ? data.count : 0;
      chat.markQueuedDelivered(cid, count);
      break;
    }
    case "spawn_done": {
      // Phase 4 — a background spawn completed. The corresponding
      // DelegateCard's cli_stream.exit_code is already set by the
      // existing cli_sink path, so the UI updates naturally; this
      // toast just lets the operator see the completion in the pulse
      // feed without scrolling chat. Keep it short.
      const data = env.data as {
        kind?: unknown;
        status?: unknown;
        summary?: unknown;
        handle?: unknown;
      };
      const kind = typeof data.kind === "string" ? data.kind : "spawn";
      const status = typeof data.status === "string" ? data.status : "done";
      const summary = typeof data.summary === "string" ? data.summary : "";
      const severity =
        status === "failed"
          ? "error"
          : status === "cancelled"
            ? "warning"
            : "info";
      useToastStore
        .getState()
        .push(`${kind} ${status}${summary ? ` — ${summary}` : ""}`, severity);
      break;
    }
    case "tasks_state": {
      const data = env.data as { items?: unknown };
      if (Array.isArray(data.items)) {
        const validated: TaskItem[] = [];
        for (const raw of data.items) {
          if (
            raw &&
            typeof raw === "object" &&
            typeof (raw as Record<string, unknown>).id === "string" &&
            typeof (raw as Record<string, unknown>).title === "string"
          ) {
            const r = raw as Record<string, unknown>;
            const status =
              r.status === "in_progress" || r.status === "completed"
                ? r.status
                : "pending";
            validated.push({
              id: r.id as string,
              title: r.title as string,
              status,
            });
          }
        }
        useTasksStore.getState().setItems(validated);
      }
      break;
    }
    case "stream_tool_call_end": {
      const data = env.data as unknown as StreamToolCallEndData;
      chat.addToolCall(cid, {
        call_id: data.call_id,
        name: data.name,
        input: data.input ?? {},
      });
      useToolActivityStore.getState().setLastTool(data.name, data.call_id);
      // Phase 4 — flag the call as background so DelegateCard renders
      // the "↻ background" badge. Only delegate_* / invoke_agent take
      // the background flag; other tools ignore it. Read defensively:
      // older envelopes / non-spawn-aware tools won't have `background`
      // in `input`, which falls through.
      const input = data.input as Record<string, unknown> | undefined;
      if (input && input.background === true) {
        chat.markCallBackground(cid, data.call_id);
      }
      break;
    }
    case "stream_tool_result": {
      const data = env.data as unknown as StreamToolResultData;
      useToolActivityStore.getState().markResult();
      chat.addToolResult(cid, {
        call_id: data.call_id,
        output: data.output,
        is_error: data.is_error,
        ...(data.metadata ? { metadata: data.metadata } : {}),
      });
      break;
    }
    case "stream_stop": {
      const data = env.data as unknown as StreamStopData;
      if (
        typeof data.input_tokens === "number" ||
        typeof data.output_tokens === "number"
      ) {
        chat.setMessageStats(cid, {
          input_tokens: data.input_tokens ?? 0,
          output_tokens: data.output_tokens ?? 0,
          cached_tokens: data.cached_tokens ?? 0,
        });
      }
      break;
    }
    case "stream_error": {
      const data = env.data as unknown as StreamErrorData;
      // Prefer `reason` (F2 envelope contract) over the legacy `message`
      // field so toast bodies surface the actual failure, not a generic
      // fallback. Falls back to `message` for envelopes emitted before the
      // F2 wiring landed.
      const msg = data.reason || data.message || "error";
      if (msg === "cancelled") {
        chat.markInterrupted(cid);
        setOrbState("idle");
        break;
      }
      if (data.severity === "warning") {
        // Operator-input miss (session not found, /load typo, /observe bad
        // mode). Toast only — orb stays calm and no red error bubble in
        // chat. Mirrors `_handleCommandResult` severity gating.
        useToastStore.getState().push(msg, "warning");
        // Self-heal ONLY when the missing conversation is the currently-
        // persisted auto-resume target. Blanket-clearing on any "session not
        // found" wipes a valid target when the operator types `/load typo` —
        // then the next reload doesn't auto-resume the good one. The message
        // carries whatever they typed, which is a title or an id, so the
        // comparison is against both.
        const notFound = /^session not found: (.+)$/.exec(msg);
        if (notFound) {
          const missing = notFound[1].trim();
          const sessions = useSessionStore.getState();
          const target = sessions.sessions.find(
            (s) => s.chat_id === sessions.lastChatId,
          );
          if (
            sessions.lastChatId === missing ||
            (target && target.title === missing)
          ) {
            sessions.setLastChatId(null);
          }
        }
        break;
      }
      if (data.severity === "soft") {
        // Tool-loop reset (chat.py: tool-iteration cap hit). The backend
        // doesn't break the turn — it zeroes the counter and keeps
        // streaming. Surface a distinct note so the operator sees the assistant
        // is still working past the cap, not a transient adapter hiccup.
        if (data.reason === "tool_cap_reset") {
          const n = typeof data.resets === "number" ? data.resets : 1;
          chat.addStreamNote(
            cid,
            `Tool-loop cap reset (#${n}) — ${useIdentityStore.getState().name || ENTITY_FALLBACK} is still working.`,
          );
          break;
        }
        // Post-commit provider hiccup (FallbackAdapter). Surface a small
        // inline note inside the active bubble so the recovery iteration's
        // text continues to render in the same message. No orb flip, no
        // toast, no red card — the runtime is already retrying.
        const provider = (data.provider_error || msg).split("\n")[0].trim();
        const tail = data.request_id ? ` (${data.request_id})` : "";
        const note = `Provider hiccup on ${data.model ?? "unknown"} — ${provider}${tail}. Retrying via fallback.`;
        chat.addStreamNote(cid, note);
        break;
      }
      chat.addError(cid, msg);
      signals?.onError();
      // Turn-scoped failure (provider down, no model configured, tool
      // blowup): the red bubble stays in the transcript as the record,
      // but the orb recovers on its own — a failed TURN is not a failed
      // TESSERACT. Connectivity errors (websocket.ts) stay persistent.
      setOrbState("error", { autoClearMs: TRANSIENT_ERROR_CLEAR_MS });
      getController()?.pulseEvent("error");
      useToastStore.getState().push(msg, "error");
      break;
    }
    case "loop_end": {
      const data = env.data as unknown as LoopEndData;
      chat.completeTurn(cid, String(data.turn), "end_turn");
      // Phase 16 S3 — `loop_end` arrives once the response stream closes,
      // but TTS audio queued from sentence-boundary `tts_chunk` envelopes
      // can still be draining for several seconds. Skip the idle flip if
      // the TtsPlayer is mid-playback; `onStateChange(false)` will fire
      // later when the queue empties and own the idle transition.
      // `ensureTtsPlayer` (NOT bare `getTtsPlayer`) because the
      // singleton's `onStateChange` callback is wired ONLY on the first
      // `ensureTtsPlayer` call. If `loop_end` arrives before any
      // `tts_chunk` has hit dispatch (typed-chat with no voice, or
      // voice-disabled session), bare `getTtsPlayer()` here would
      // construct the singleton with empty opts; subsequent
      // `ensureTtsPlayer()` calls would short-circuit on the wired flag
      // and silently return the bare instance — orb stays stuck on
      // `speaking` because `onStateChange(false)` is wired to nothing.
      const ttsActive = ensureTtsPlayer().isSpeaking;
      // Re-read the live entity state — the function-top snapshot was
      // taken before any `setOrbState` call in this handler, and a
      // synchronous flip earlier in the same task would leave it stale.
      const liveEntityState = useEntityStore.getState().state;
      if (
        !ttsActive &&
        (liveEntityState === "speaking" ||
          liveEntityState === "thinking" ||
          liveEntityState === "spawning")
      ) {
        setOrbState("idle");
      }
      signals?.onReset();
      getController()?.pulseEvent("success");
      break;
    }
    default:
      console.debug("[dispatch] unhandled loop type:", env.type);
  }
}
