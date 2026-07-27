import type { Envelope, EnvelopeCategory } from "../lib/types";
import { isSyntheticTurn } from "../lib/types";
import { getController } from "../lib/entity/registry";
import { useActivityStore } from "./activity";
import { useAutonomyStore } from "./autonomy";
import { usePulseStore } from "./pulse";
import { useSurfacesStore } from "./surfaces";
import { handleBackground } from "./dispatch/background";
import { handleChat } from "./dispatch/chat";
import { handleCli } from "./dispatch/cli";
import { handleCommand, handleCommandResult } from "./dispatch/command";
import { handleCost } from "./dispatch/cost";
import { handleEntity } from "./dispatch/entity";
import { handleExecution } from "./dispatch/execution";
import { handleLoop } from "./dispatch/loop";
import { handleRouting } from "./dispatch/routing";
import { handleSchedule } from "./dispatch/schedule";
import { handleSession } from "./dispatch/session";
import { handleVoice } from "./dispatch/voice";
import { handleWorkspace } from "./dispatch/workspace";

interface DispatchOpts {
  fromCatchup?: boolean;
}

// rAF-gated pulse stream_text buffer. Concatenates the `delta` strings
// of consecutive same-frame stream_text envelopes into one synthetic
// envelope, which the pulse store then coalesces into its one-per-turn
// row exactly as before. Net effect: per-frame `set()` instead of
// per-chunk `set()` — pulse panel still shows the live tail, just
// without re-rendering 50× a second.
let _pulseStreamBuffer: { env: Envelope; deltaText: string } | null = null;
let _pulseRafHandle: number | null = null;

function _bufferPulseStreamText(env: Envelope): void {
  const delta = String((env.data as { delta?: unknown })?.delta ?? "");
  if (_pulseStreamBuffer === null) {
    _pulseStreamBuffer = { env, deltaText: delta };
  } else {
    _pulseStreamBuffer.deltaText += delta;
    // Use the latest envelope's metadata (timestamp etc) so the
    // pulse row's `last_ts` reflects the most recent chunk.
    _pulseStreamBuffer.env = env;
  }
  if (_pulseRafHandle !== null) return;
  const raf =
    typeof requestAnimationFrame === "function"
      ? requestAnimationFrame
      : (cb: FrameRequestCallback) =>
          setTimeout(() => cb(performance.now()), 16) as unknown as number;
  _pulseRafHandle = raf(() => {
    _pulseRafHandle = null;
    _flushPulseStreamBuffer();
  });
}

function _flushPulseStreamBuffer(): void {
  if (_pulseStreamBuffer === null) return;
  const { env, deltaText } = _pulseStreamBuffer;
  _pulseStreamBuffer = null;
  if (_pulseRafHandle !== null && typeof cancelAnimationFrame === "function") {
    cancelAnimationFrame(_pulseRafHandle);
    _pulseRafHandle = null;
  }
  const merged: Envelope = {
    ...env,
    data: { ...(env.data as Record<string, unknown>), delta: deltaText },
  };
  usePulseStore.getState().push(merged);
}

export function handleEnvelope(env: Envelope, opts: DispatchOpts = {}): void {
  const signals = opts.fromCatchup
    ? null
    : (getController()?.getSignals() ?? null);

  // WP-2: synthetic workspace turns reply via `workspace_reply`; the
  // reply renders in the comment thread (via the parallel
  // `workspace_comment_appended` broadcast — no turn_id, always passes).
  // Every other envelope from a synthetic turn (loop_start/end,
  // stream_tool_call_*, stream_tool_result, entity_signals, …) would
  // otherwise pollute the chat panel + orb. Drop them here so the chat
  // surface stays exclusively the chat turn's. Workspace-category
  // broadcasts always pass through.
  if (isSyntheticTurn(env) && env.category !== "workspace") {
    return;
  }

  // Catchup-replay path: flush any live pulse-stream buffer FIRST so a
  // pending merge from the previous live turn can't tail-attach onto a
  // catchup envelope and inherit the wrong turn_id. (Catchup itself
  // bypasses pulse — see below — so the buffer would otherwise sit
  // until the next live envelope, which is the leak the reviewer flagged.)
  if (opts.fromCatchup) {
    _flushPulseStreamBuffer();
  }

  // Pulse store coalesces consecutive `stream_text` deltas into one
  // row, but a Zustand `set()` still fires per delta — every pulse
  // subscriber re-renders at chunk cadence. rAF-buffer the
  // stream_text feed so the pulse panel updates at most once per
  // frame; everything else (turn boundaries, errors, tool events)
  // still goes through immediately.
  if (!opts.fromCatchup) {
    if (env.type === "stream_text") {
      _bufferPulseStreamText(env);
    } else {
      _flushPulseStreamBuffer();
      usePulseStore.getState().push(env);
    }
  }

  switch (env.category as EnvelopeCategory) {
    case "session":
      handleSession(env);
      break;
    case "loop":
      handleLoop(env, signals);
      break;
    case "execution":
      handleExecution(env);
      break;
    case "cli":
      handleCli(env);
      break;
    case "routing":
      handleRouting(env);
      break;
    case "entity":
      handleEntity(env, signals);
      break;
    case "background":
      handleBackground(env);
      break;
    case "command_result":
      handleCommandResult(env);
      break;
    case "command":
      handleCommand(env);
      break;
    case "workspace":
      handleWorkspace(env);
      // AU-7 S1 — recovery_summary workspace events drive the
      // RecoveryPane on the autonomy dashboard.
      useAutonomyStore.getState().applyEnvelope(env);
      break;
    case "agenda":
      // agenda_item_added / _transitioned / _updated — emitted by
      // routes/agenda.py after each store mutation so the operator's
      // Autonomy tab refreshes without polling.
      useAutonomyStore.getState().applyEnvelope(env);
      break;
    case "workers":
      // worker_record_started / _transitioned / _archived — fired from
      // workers/record.py::write_record + archive_record. Autonomy
      // store's existing `applyEnvelope` already has matching `case`
      // branches that trigger fetchWorkers(); no extra wiring needed.
      useAutonomyStore.getState().applyEnvelope(env);
      break;
    case "governor":
      // governor_pause_added / _removed / governor_tick — fired from
      // autonomy/governor.py::PauseStore + Governor.run_once. The
      // autonomy store already has matching `case` branches that
      // trigger fetchGovernor().
      useAutonomyStore.getState().applyEnvelope(env);
      break;
    case "schedule":
      handleSchedule(env);
      break;
    case "cost":
      handleCost(env);
      break;
    case "voice":
      handleVoice(env);
      break;
    case "canvas":
      // Y-2 — Surface Protocol events (surface_created / _updated / _moved /
      // _closed / …) re-keyed from the `surface` background-bus channel.
      // session_id carries the view name.
      useSurfacesStore.getState().applyEnvelope(env);
      break;
    case "activity":
      // AS-2 — Unified Activity Registry deltas (registered/updated/removed)
      // re-keyed from the `activity` background-bus channel. session_id carries
      // the activity_id.
      useActivityStore.getState().applyEnvelope(env);
      break;
    case "chat":
      // mirror-multi-chat P3 — chat lifecycle (create/switch/archive) drives
      // the tab strip's conversation slices.
      handleChat(env);
      break;
    default:
      console.debug("[dispatch] unhandled category:", env.category, env.type);
  }
}
