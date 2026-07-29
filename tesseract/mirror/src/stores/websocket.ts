import { create } from "zustand";
import { isEnvelope } from "../lib/envelope";
import { fetchEventsSince } from "../lib/api";
import { WS_URL } from "../lib/endpoints";
import { nextDelay } from "../lib/backoff";
import { useSoulStore } from "./soul";
import { useHealthStore } from "./health";
import { useIdentityStore } from "./identity";
import { useConversationStore } from "./conversation";
import { useSessionStore } from "./session";
import { handleEnvelope } from "./dispatch";
import { setOrbState } from "./dispatch/orb";
import { useTerminalStore } from "./terminal";
import { useObserverStore } from "./observer";
import { useToastStore } from "./toasts";
import { useAlarmsStore } from "./alarms";
import { useScheduleStore } from "./schedule";
import { useConscienceStore } from "./conscience";
import { useAgentsStore } from "./agents";
import { useAutonomyStore } from "./autonomy";
import { useActivityStore } from "./activity";
import { useToolsStore } from "./tools";
import {
  describeResumeCutoff,
  isWithinResumeCutoff,
  useSessionPolicyStore,
} from "./sessionPolicy";

const MAX_RECONNECT_BEFORE_ERROR = 5;

interface WebSocketState {
  status:
    | "connecting"
    | "connected"
    | "reconnecting"
    | "disconnected"
    | "error";
  reconnectAttempt: number;
  sessionId: string | null;
  lastEventTs: string | null;
  connect: () => void;
  disconnect: () => void;
  sendMessage: (type: string, data: Record<string, unknown>) => void;
  sendRaw: (obj: Record<string, unknown>) => void;
  sendBinary: (buffer: ArrayBufferLike) => void;
  setSessionId: (id: string) => void;
}

export const useWebSocketStore = create<WebSocketState>((set, get) => {
  let _socket: WebSocket | null = null;
  let _reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let _intentionalClose = false;
  // One-shot guard: auto-resume from persisted saveName fires only on the
  // first successful connection per page load. Without this, a reconnect
  // storm (e.g. WS drops before the first stream_error round-trip completes)
  // would re-dispatch /resume against a still-stale saveName — producing
  // duplicate "session not found" entries in the pulse feed.
  let _autoResumeAttempted = false;
  // Live-gate fix pass (Finding 2, 2026-07-05) — one-shot guard mirroring
  // `_autoResumeAttempted`: the terminal store's `bootstrapPanes()` already
  // handles the FIRST successful connection per page load (via TerminalView's
  // mount effect, independent of WS timing). Every connection AFTER that one
  // is a reconnect — most commonly the Mirror backend restarting — and must
  // re-run the per-pane terminal handshake, since a page reload never
  // happened to trigger `bootstrapPanes` again and the pane was otherwise
  // left dead (no respawn, keystrokes dropped, no visual indication).
  let _hasConnectedOnce = false;
  // Pending-send buffer (2026-05-15): if the TerminalView bootstrap fires
  // `addTab()` while the WS is still CONNECTING, the `terminal_start`
  // message was silently dropped and the operator saw an empty pane that
  // only worked after close+reopen. Buffer non-binary sendRaw calls
  // during CONNECTING, flush on open. Cap is a generous 64 to absorb a
  // bursty bootstrap without unbounded growth on a permanently-down WS.
  const _pendingSends: Record<string, unknown>[] = [];
  const _PENDING_CAP = 64;

  function _isCurrentSocket(ws: WebSocket): boolean {
    return _socket === ws;
  }

  async function _catchUp(
    sessionId: string | null,
    lastEventTs: string | null,
  ): Promise<void> {
    if (sessionId === null || lastEventTs === null) return;
    try {
      const events = await fetchEventsSince(sessionId, lastEventTs, 5000);
      events.forEach((env) => handleEnvelope(env, { fromCatchup: true }));
    } catch {
      // silent — catch-up failure is non-fatal
    }
  }

  function _scheduleReconnect(attempt: number): void {
    const delay = nextDelay(attempt);
    console.debug(`[ws] reconnect in ${delay}ms (attempt ${attempt})`);
    _reconnectTimer = setTimeout(() => {
      useWebSocketStore.getState().connect();
    }, delay);
  }

  return {
    status: "disconnected",
    reconnectAttempt: 0,
    sessionId: null,
    lastEventTs: null,

    connect: () => {
      const { status } = get();
      if (status === "connecting") return;
      if (
        _socket !== null &&
        (_socket.readyState === WebSocket.CONNECTING ||
          _socket.readyState === WebSocket.OPEN)
      )
        return;

      if (_reconnectTimer !== null) {
        clearTimeout(_reconnectTimer);
        _reconnectTimer = null;
      }

      _intentionalClose = false;
      set({ status: "connecting" });

      const ws = new WebSocket(WS_URL);
      _socket = ws;

      ws.onopen = () => {
        if (!_isCurrentSocket(ws)) return;
        const prev = get();
        set({ status: "connected", reconnectAttempt: 0 });
        // Flush messages queued while the WS was CONNECTING (e.g. the
        // terminal bootstrap's `terminal_start` for the auto-opened tab).
        if (_pendingSends.length > 0) {
          const drained = _pendingSends.splice(0, _pendingSends.length);
          for (const obj of drained) {
            try {
              ws.send(JSON.stringify(obj));
            } catch (err) {
              console.debug("[ws] pending flush failed", err);
            }
          }
        }
        // Live-gate fix pass (Finding 2) — re-run the terminal pane
        // handshake on every RECONNECT (backend restart, brief network
        // drop) but not the very first connect of this page load, which
        // `TerminalView`'s mount effect already covers via
        // `bootstrapPanes()`. Firing on both would double-send
        // `terminal_reattach` for the same panes.
        if (_hasConnectedOnce) {
          useTerminalStore.getState().reattachAfterReconnect();
        } else {
          _hasConnectedOnce = true;
        }
        // Auto-recovery on every successful connect (first connect +
        // reconnects). Each store's fetcher handles its own errors and
        // sets `lastError` — one tab's failure does not affect others.
        // Without this sweep, "Mirror started before backend" leaves
        // alarms/schedule/conscience tabs stuck on the initial
        // failed fetch until the operator clicks refresh.
        useSoulStore.getState().fetchSoul();
        useHealthStore.getState().fetchBreakers();
        useIdentityStore.getState().fetchIdentity();
        useAlarmsStore.getState().fetchAlarms();
        useScheduleStore.getState().fetchJobs();
        useConscienceStore.getState().fetchDrift();
        useAgentsStore.getState().fetchAll();
        // tools.load() guards against re-fetch when `tools !== null` —
        // pass force=true so reconnect after a failed first load can
        // actually replace the stale state.
        useToolsStore.getState().load(true);
        // Fix-pass D1 (Codex #7): pty_manager.cleanup_for_ws resets
        // app["observer_state"]="off" on WS disconnect. Without this
        // sync, the frontend keeps showing armed/observing against a
        // disarmed backend until the next manual interaction.
        useObserverStore.getState().syncFromBackend();
        // AU-7 S1 — hydrate the autonomy dashboard on every connect.
        // fetchAll() resolves Promise.allSettled internally, so a slow
        // or down sub-endpoint doesn't gate the others.
        void useAutonomyStore.getState().fetchAll();
        // AS-2 — load the activity snapshot so pre-connect entries are visible.
        void useActivityStore.getState().hydrate();
        _catchUp(prev.sessionId, prev.lastEventTs);
        if (!_autoResumeAttempted) {
          const persistedName = useSessionStore.getState().saveName;
          if (persistedName && _socket?.readyState === WebSocket.OPEN) {
            _autoResumeAttempted = true;
            // Phase 18 Task C — load resume policy BEFORE the cutoff
            // check fires so isWithinResumeCutoff reads the operator-
            // configured value, not the baked-in Phase 15 default.
            // The session list is also refreshed so a fresh tab doesn't
            // decide on stale state.
            const policyReady = useSessionPolicyStore.getState().loaded
              ? Promise.resolve()
              : useSessionPolicyStore.getState().fetch();
            void Promise.all([
              policyReady,
              useSessionStore.getState().fetchList(),
            ]).then(() => {
              const sessions = useSessionStore.getState().sessions;
              const match = sessions.find(
                (s) => s.session_id === persistedName,
              );
              if (match && isWithinResumeCutoff(match.started_at)) {
                if (_socket?.readyState === WebSocket.OPEN) {
                  _socket.send(
                    JSON.stringify({
                      type: "command",
                      data: { cmd: `/resume ${persistedName}` },
                    }),
                  );
                }
              } else if (!match) {
                // Saved name no longer exists on disk — clear the persisted
                // pointer so we don't keep retrying every reconnect.
                useSessionStore.getState().setSaveName(null);
              } else {
                // Outside the resume cutoff — leave saveName intact so the
                // operator can still see + manually /load it from the
                // SessionDrawer, but surface a toast so they know why the
                // session didn't auto-resume. Phase 18 audit m1 — message
                // now reflects the actual policy in force.
                useToastStore
                  .getState()
                  .push(
                    `Session "${persistedName}" is ${describeResumeCutoff()} — open the Sessions drawer to /load manually.`,
                    "info",
                    6000,
                  );
              }
            });
          }
        }
      };

      ws.onmessage = (event: MessageEvent) => {
        if (!_isCurrentSocket(ws)) return;
        let raw: unknown;
        try {
          raw = JSON.parse(event.data as string);
        } catch {
          console.warn("[ws] failed to parse message");
          return;
        }
        // Terminal messages are sent raw (not enveloped) by the backend (Phase 9).
        const rawType = (raw as Record<string, unknown>).type;
        if (
          rawType === "terminal_output_chunk" ||
          rawType === "terminal_started" ||
          rawType === "terminal_stopped" ||
          rawType === "terminal_error" ||
          rawType === "terminal_observer_status" ||
          rawType === "terminal_reattached" ||
          rawType === "terminal_reattach_failed"
        ) {
          useTerminalStore
            .getState()
            .handleRawMessage(raw as Record<string, unknown>);
          return;
        }

        // Some backend channels ship a `{kind, channel, session_id, ts, data}`
        // shape (events.py) instead of the standard Envelope. Re-key to the
        // standard `{type, category, ..., timestamp}` Envelope before dispatch
        // so they flow through the same handler chain.
        const rec = raw as Record<string, unknown>;
        if (rec && rec.channel === "surface" && typeof rec.kind === "string") {
          // Y-2 — Surface Protocol events. Re-key to the standard Envelope
          // with category 'canvas'; session_id carries the view name (see
          // orchestrator/surfaces/events.py).
          raw = {
            type: rec.kind,
            category: "canvas",
            session_id:
              typeof rec.session_id === "string" ? rec.session_id : "",
            timestamp:
              typeof rec.ts === "string" ? rec.ts : new Date().toISOString(),
            data: (rec.data ?? {}) as Record<string, unknown>,
          };
        } else if (
          rec &&
          rec.channel === "activity" &&
          typeof rec.kind === "string"
        ) {
          // AS-1/AS-2 — Unified Activity events. Re-key to the standard
          // Envelope with category 'activity'; session_id carries the
          // activity_id (orchestrator/activity/events.py). Replay is dropped at
          // the WS pump — GET /api/activity is the catch-up path.
          raw = {
            type: rec.kind,
            category: "activity",
            session_id:
              typeof rec.session_id === "string" ? rec.session_id : "",
            timestamp:
              typeof rec.ts === "string" ? rec.ts : new Date().toISOString(),
            data: (rec.data ?? {}) as Record<string, unknown>,
          };
        }

        if (!isEnvelope(raw)) {
          console.warn("[ws] non-envelope message received");
          return;
        }
        set({ lastEventTs: raw.timestamp });
        handleEnvelope(raw);
      };

      ws.onclose = (event) => {
        if (!_isCurrentSocket(ws)) return;
        _socket = null;
        if (_intentionalClose) {
          _intentionalClose = false;
          return;
        }
        // Interrupt EVERY in-flight turn on an unexpected close. inc.C2:
        // background chats stream in parallel, so a disconnect can strand
        // turns across multiple slices — sweep them all, not just the active one.
        if (get().status === "connected") {
          useConversationStore.getState().interruptAllStreaming();
        }
        // 1013 TRY_AGAIN_LATER = the backend deliberately closed us because chat
        // infra is still booting (~20-30s). Reconnect WITHOUT advancing the
        // error counter so the boot window never trips a false `error` orb.
        if (event.code === 1013) {
          set({ status: "reconnecting", reconnectAttempt: 0 });
          _scheduleReconnect(0);
          return;
        }
        const { reconnectAttempt } = get();
        const next = reconnectAttempt + 1;
        if (next >= MAX_RECONNECT_BEFORE_ERROR) {
          // Through setOrbState (no autoClearMs), NOT a raw store write:
          // going through the dispatcher cancels any pending transient-error
          // expiry, so a turn-error's 5s timer can never clear this
          // persistent backend-unreachable state.
          setOrbState("error");
        }
        set({ status: "reconnecting", reconnectAttempt: next });
        _scheduleReconnect(next);
      };

      ws.onerror = () => {
        if (!_isCurrentSocket(ws)) return;
        console.debug("[ws] error event");
      };
    },

    disconnect: () => {
      _intentionalClose = true;
      if (_reconnectTimer !== null) {
        clearTimeout(_reconnectTimer);
        _reconnectTimer = null;
      }
      if (_socket !== null) {
        const current = _socket;
        _socket = null;
        current.close();
      }
      set({ status: "disconnected", reconnectAttempt: 0 });
    },

    sendMessage: (type: string, data: Record<string, unknown>) => {
      if (_socket?.readyState === WebSocket.OPEN) {
        _socket.send(JSON.stringify({ type, data }));
      } else {
        console.debug("[ws] sendMessage called but socket not open");
      }
    },

    sendRaw: (obj: Record<string, unknown>) => {
      if (_socket?.readyState === WebSocket.OPEN) {
        _socket.send(JSON.stringify(obj));
        return;
      }
      // Buffer while CONNECTING so the bootstrap's first terminal_start
      // doesn't get dropped on a fresh page load. Anything else (closed
      // / closing socket, capped buffer) is logged and dropped — flushing
      // half a session's commands into a fresh reconnect would be worse
      // than the drop.
      if (_socket?.readyState === WebSocket.CONNECTING) {
        if (_pendingSends.length < _PENDING_CAP) {
          _pendingSends.push(obj);
        } else {
          console.debug("[ws] pending buffer full, dropping send");
        }
        return;
      }
      console.debug("[ws] sendRaw called but socket not open");
    },

    sendBinary: (buffer: ArrayBufferLike) => {
      // Used by `lib/voice/stt-stream.ts` to forward 16 kHz Int16 PCM
      // frames; backend `ws.py` `WSMsgType.BINARY` arm appends them to
      // the per-session voice_pcm_buffer.
      if (_socket?.readyState === WebSocket.OPEN) {
        _socket.send(buffer);
      } else {
        console.debug("[ws] sendBinary called but socket not open");
      }
    },

    setSessionId: (id: string) => {
      set({ sessionId: id });
    },
  };
});
