import type { MapLiveness } from "../lib/api";
import type { Envelope, EnvelopeCategory } from "../lib/types";
import { isSyntheticTurn } from "../lib/types";
import { getController } from "../lib/entity/registry";
import { useActivityStore } from "./activity";
import { useAutonomyStore } from "./autonomy";
import { usePulseStore } from "./pulse";
import { useLivenessStore } from "./liveness";
import { useStaleStore } from "./stale";
import { useSurfacesStore } from "./surfaces";
import { handleBackground } from "./dispatch/background";
import { handleChat } from "./dispatch/chat";
import { handleCli } from "./dispatch/cli";
import { handleCommand, handleCommandResult } from "./dispatch/command";
import { handleCost } from "./dispatch/cost";
import { handleSurfaceCommand } from "../canvas/commands";
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

// Autonomy's Overview room lists every running unit off the same registry
// these envelopes come from, so anything that changes one is what tells the
// room to read them again.
//
// Its own clock is a minute, which was right while the band held only
// background work and is wrong now that it holds a conversation turn. A turn
// is the shortest lived thing in the runtime and the only one somebody is
// actively waiting on: it opens and closes well inside one poll, so the person
// watching the panel sees nothing at all. Measured on the live machine
// 2026-09-02, where the payload carried a running turn and the panel said
// "Nothing is running" four seconds later.
//
// Coalesced to one read a second, and the same window serves the Health room's
// own re-read below: both are "something changed, go and read the row again",
// and a person cannot tell 4 Hz from 1 Hz on either. The liveness contract's
// 4 Hz is a CEILING on
// visual updates, not a target, and this route is not free: the event-loop-lag
// sampler catches it doing its own file reads inside the request
// (`list_active_records`, `_config_roots`, under `cors_middleware`). A turn
// beats at every step boundary and a fast tool loop crosses several a second,
// so the window is what bounds this to one request per second while a turn
// runs and to nothing at all while none does. A person waiting on an answer
// cannot tell 4 Hz from 1 Hz; the loop can.
const _ROOM_REREAD_COALESCE_MS = 1_000;
let _overviewRereadHandle: ReturnType<typeof setTimeout> | null = null;

function rereadOverviewSoon(): void {
  // Only once the room has read it. A re-read of nothing is a request from
  // every session that never opened the panel, which is the guard the Managed
  // room's re-read already makes for the same reason.
  if (useAutonomyStore.getState().overview.lastFetched === null) return;
  if (_overviewRereadHandle !== null) return;
  _overviewRereadHandle = setTimeout(() => {
    _overviewRereadHandle = null;
    void useAutonomyStore.getState().fetchOverview();
  }, _ROOM_REREAD_COALESCE_MS);
}

// The rooms whose rows carry more than a state, and what tells them to read
// the rest of the row. A department's state arrives on the socket; its
// sentence, its number and the band it is grouped under do not, because those
// are the row rather than the liveness.
const _HEALTH_KEY = "department:";

/** The states a surface has a rendering for. The backend's own vocabulary,
 *  held here because a pushed value is the one thing on this panel that does
 *  not arrive through a typed fetch. */
const RENDERABLE: ReadonlySet<string> = new Set([
  "running",
  "idle",
  "pending",
  "degraded",
  "failed",
  "refused",
  "not_instrumented",
  "unknown",
]);
let _healthRereadHandle: ReturnType<typeof setTimeout> | null = null;

function rereadHealthSoon(): void {
  // Only once the room has read it, for the reason the overview re-read gives:
  // a re-read of nothing is a request from every session that never opened the
  // panel.
  if (useAutonomyStore.getState().health.lastFetched === null) return;
  if (_healthRereadHandle !== null) return;
  _healthRereadHandle = setTimeout(() => {
    _healthRereadHandle = null;
    void useAutonomyStore.getState().fetchHealth();
  }, _ROOM_REREAD_COALESCE_MS);
}

function applyLiveness(env: Envelope): void {
  const data = env.data as {
    seq?: unknown;
    full?: unknown;
    sweepSeconds?: unknown;
    states?: Record<string, unknown>;
  };
  if (typeof data.seq !== "number" || typeof data.states !== "object") return;
  // Only what a surface can actually paint. A value carrying a `state` this
  // app has no rendering for would be spread onto a row and drawn as a class
  // nothing styles, which reads as nothing being wrong.
  const held = useLivenessStore.getState().states;
  const states: Record<string, MapLiveness> = {};
  let changedDepartment = false;
  for (const [key, value] of Object.entries(data.states ?? {})) {
    const state = (value as { state?: unknown } | null)?.state;
    if (typeof state !== "string" || !RENDERABLE.has(state)) continue;
    states[key] = value as MapLiveness;
    if (key.startsWith(_HEALTH_KEY) && held[key]?.liveness.state !== state) {
      changedDepartment = true;
    }
  }
  const full = data.full === true;
  useLivenessStore
    .getState()
    .apply(
      data.seq,
      full,
      states,
      typeof data.sweepSeconds === "number" ? data.sweepSeconds : undefined,
    );
  // The state arrives on the socket; the sentence, the number and the band a
  // row is grouped under arrive with the room's own read. Asked for only when
  // a department's state actually MOVED, compared against what was held: the
  // sweep says everything every time, so re-reading on every message with a
  // department in it made the room poll at the sweep's cadence, which is what
  // this feed exists to stop.
  if (changedDepartment) rereadHealthSoon();
}

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
      //
      // `surface_command` is the one kind that is not a change to the map: it
      // has to reach the renderer instance that owns the card and can act on
      // its element, so it goes to the command store rather than the reducer.
      if (env.type === "surface_command") {
        // `session_id` carries the view on this channel.
        handleSurfaceCommand(env.data, env.session_id ?? "orb");
      } else {
        useSurfacesStore.getState().applyEnvelope(env);
      }
      break;
    case "activity":
      // AS-2 — Unified Activity Registry deltas (registered/updated/removed)
      // re-keyed from the `activity` background-bus channel. session_id carries
      // the activity_id.
      useActivityStore.getState().applyEnvelope(env);
      rereadOverviewSoon();
      break;
    case "chat":
      // mirror-multi-chat P3 — chat lifecycle (create/switch/archive) drives
      // the tab strip's conversation slices.
      handleChat(env);
      break;
    case "panel":
      // AC-6 — the backend wrote something a panel is showing and named the
      // cache key for it (orchestrator/panel_refresh.py). Every
      // `useCachedFetch` holding that key refetches; nothing here knows which
      // panel that is, which is why one envelope covers all of them.
      if (typeof env.data?.key === "string") {
        useStaleStore.getState().markStale(env.data.key);
      }
      // AR-31 — the runtime says a thing the Autonomy panel draws is in a
      // different state (mirror/server/liveness_feed.py). The state is
      // applied without a fetch; a department also has a sentence, a number
      // and a band, and those come with the room's own next read, which this
      // asks for at once rather than at the end of its poll.
      if (env.type === "liveness_changed") {
        applyLiveness(env);
      }
      break;
    default:
      console.debug("[dispatch] unhandled category:", env.category, env.type);
  }
}
