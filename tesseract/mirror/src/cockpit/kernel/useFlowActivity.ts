import { useEffect, useState } from 'react';
import { useEntityStore } from '../../stores/entity';
import { useToolActivityStore } from '../../stores/toolActivity';
import { useVoiceStore } from '../../stores/voice';
import { flowById, type FlowNode } from './flows';
import type { EntityState } from '../../lib/types';

// One cursor per flow, and a trail behind it. The old synapse column lit a
// single node and moved it, which is what made the panel worth watching; the
// set-of-lit-nodes model that replaced it left `persist history` and `mic`
// glowing in a completely idle app.
//
// Signals, not node ids. The manifest declares a `signal` per node and this
// hook resolves it — so wiring a new stage means emitting its signal, not
// editing a table here. It used to key off ids, which quietly meant five
// declared signals had no effect at all.
//
// The trail is not simply everything above the cursor. Depth-0 nodes are the
// spine every run walks, so those do trail; anything deeper is a branch — the
// STT lane, a tool group, the TTS lane — and only counts as passed once it
// has actually been the cursor. Otherwise a typed message lights the
// microphone.

const FLASH_MS = 220;
const TOOL_WINDOW_MS = 4000;

/**
 * Signals something in the frontend actually emits today. A node declaring
 * anything else is drawn as UNWIRED rather than cold, because "no producer"
 * and "the run did not reach here" look identical otherwise, and one of them
 * is a lie. `tool.permission`, `memory.bm25`, `memory.vector` and
 * `delegate.stream` need the backend to emit them before they can light.
 */
export const WIRED_SIGNALS: ReadonlySet<string> = new Set([
  'turn.start',
  'turn.stream',
  'turn.persist',
  'voice.stt',
  'voice.tts',
  'tool.dispatch',
  'tool.result',
  'observer.fire',
  'memory.injected',
  'delegate.spawn',
]);

/** Entity state → the signal it stands for. Idle means no turn: no cursor. */
const ENTITY_TO_SIGNAL: Partial<Record<EntityState, string>> = {
  thinking: 'turn.stream',
  // `speaking` is the orb's word for "emitting a reply", set on every
  // `stream_text` delta (dispatch/loop.ts) whether or not a word is spoken.
  // Mapping it to voice.tts lit the TTS lane on typed turns with TTS off.
  // The truthful source for that lane is the TTS player itself, which lands
  // in VOICE_TO_SIGNAL as `speaking_back`.
  speaking: 'turn.stream',
  spawning: 'tool.dispatch',
  council: 'tool.dispatch',
  deep_focus: 'turn.stream',
  error: 'turn.persist',
  dreaming: 'observer.fire',
};

/** Voice UI state → the signal it stands for. Voice wins: it is upstream.
 *
 *  Every key is a real `VoiceUiState`. Two that were here — `thinking` and
 *  `speaking` — are not members of that union and could never match, the same
 *  defect as a node declaring a signal nothing emits. `speaking_back` is the
 *  one the TTS player actually sets, so it is the only honest source for the
 *  TTS lane. */
const VOICE_TO_SIGNAL: Record<string, string> = {
  listening: 'voice.stt',
  speaking_in: 'voice.stt',
  transcribing: 'voice.stt',
  speaking_back: 'voice.tts',
};

/** Tools that stand for a signal outside the turn's own tool loop. */
const TOOL_TO_SIGNAL: Record<string, string> = {
  memory_search: 'memory.injected',
  memory_save: 'memory.injected',
  delegate_coder: 'delegate.spawn',
  delegate_auditor: 'delegate.spawn',
};

export interface FlowActivity {
  /** The node the flow is at right now, or null when the flow is cold. */
  cursor: string | null;
  /** Nodes this run already passed, drawn dimmer than the cursor. */
  trail: Set<string>;
  /** Nodes whose declared signal has no producer yet. */
  unwired: Set<string>;
  /** True while the newest signal is still flashing. */
  flashing: boolean;
  /** Fires this session per node that files tools under it. */
  counts: Record<string, number>;
}

/** The tool fired within the last window, or null once it goes stale. */
function useRecentTool(): { name: string | null; resulted: boolean } {
  const lastTool = useToolActivityStore((s) => s.lastTool);
  const firedAt = useToolActivityStore((s) => s.firedAt);
  const resultAt = useToolActivityStore((s) => s.resultAt);
  const [stale, setStale] = useState(false);

  useEffect(() => {
    if (!firedAt) return;
    setStale(false);
    const elapsed = Date.now() - firedAt;
    if (elapsed >= TOOL_WINDOW_MS) {
      setStale(true);
      return;
    }
    const timer = window.setTimeout(() => setStale(true), TOOL_WINDOW_MS - elapsed);
    return () => window.clearTimeout(timer);
  }, [firedAt, lastTool]);

  if (stale) return { name: null, resulted: false };
  // `>=`, not `>`: a tool that returns within the same millisecond it was
  // called has still returned. `resultAt > 0` is what keeps the initial
  // both-zero state from reading as a result nobody asked for.
  return { name: lastTool, resulted: resultAt > 0 && resultAt >= firedAt };
}

/** True for FLASH_MS after `value` changes. */
function useFlash(value: unknown): boolean {
  const [flashing, setFlashing] = useState(false);

  useEffect(() => {
    setFlashing(true);
    const timer = window.setTimeout(() => setFlashing(false), FLASH_MS);
    return () => window.clearTimeout(timer);
  }, [value]);

  return flashing;
}

/**
 * Branch nodes the cursor has occupied since the flow last went cold.
 * Accumulated in an effect, never during render: a render React discards
 * would otherwise leave a node in the trail that was never committed.
 */
function useVisited(cursor: string | null): Set<string> {
  const [visited, setVisited] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    if (cursor === null) {
      setVisited((prev) => (prev.size === 0 ? prev : new Set()));
      return;
    }
    setVisited((prev) => (prev.has(cursor) ? prev : new Set(prev).add(cursor)));
  }, [cursor]);

  return visited;
}

/** The node in this flow that carries `signal`, if any. */
function nodeFor(nodes: FlowNode[], signal: string | null): FlowNode | undefined {
  if (!signal) return undefined;
  return nodes.find((n) => n.signal === signal);
}

export function useFlowActivity(flowId: string): FlowActivity {
  const entityState = useEntityStore((s) => s.state);
  const voiceState = useVoiceStore((s) => s.state);
  const counts = useToolActivityStore((s) => s.counts);
  const recent = useRecentTool();

  const flow = flowById(flowId);
  const nodes = flow?.nodes ?? [];

  let signal: string | null = null;
  let cursor: string | null = null;

  if (flowId === 'turn') {
    signal = VOICE_TO_SIGNAL[voiceState] ?? ENTITY_TO_SIGNAL[entityState] ?? null;
    if (recent.name) {
      // A tool in flight puts the turn inside the loop under `stream`: on the
      // group that claims the tool, then on `result` once one comes back.
      const group = nodes.find((n) => n.tools?.includes(recent.name as string));
      signal = recent.resulted ? 'tool.result' : 'tool.dispatch';
      cursor = recent.resulted ? null : group?.id ?? null;
    }
  } else if (recent.name) {
    const hit = TOOL_TO_SIGNAL[recent.name];
    if (hit) signal = hit;
  }

  if (cursor === null) cursor = nodeFor(nodes, signal)?.id ?? null;

  const flashing = useFlash(cursor);
  const visited = useVisited(cursor);

  const trail = new Set(visited);
  if (cursor) {
    trail.delete(cursor);
    for (const node of nodes) {
      if (node.id === cursor) break;
      if (!node.depth) trail.add(node.id);
    }
  }

  const unwired = new Set<string>();
  const nodeCounts: Record<string, number> = {};
  for (const node of nodes) {
    // Only branches. A depth-0 stage is on the spine, so its state is a sound
    // inference from where the cursor is — the run demonstrably passed
    // through it. A branch cannot be inferred: without a signal there is no
    // way to know, and dimming it would assert it was skipped.
    if (node.depth && node.signal && !WIRED_SIGNALS.has(node.signal)) {
      unwired.add(node.id);
    }
    if (!node.tools) continue;
    const total = node.tools.reduce((sum, t) => sum + (counts[t] ?? 0), 0);
    if (total > 0) nodeCounts[node.id] = total;
  }

  return { cursor, trail, unwired, flashing, counts: nodeCounts };
}
