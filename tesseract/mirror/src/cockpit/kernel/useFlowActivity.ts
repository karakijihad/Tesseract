import { useEffect, useRef, useState } from 'react';
import { useEntityStore } from '../../stores/entity';
import { useToolActivityStore } from '../../stores/toolActivity';
import { useVoiceStore } from '../../stores/voice';
import type { EntityState } from '../../lib/types';

// What lights a node. The old synapse column mapped entity state to one node
// and flashed the tool that just fired; that behaviour is what made the map
// worth having, so it is kept and widened to the other flows.

const FLASH_MS = 220;

/** Entity state → the turn stage it means. */
const ENTITY_TO_NODE: Record<EntityState, string> = {
  idle: 'persist',
  listening: 'input',
  thinking: 'stream',
  speaking: 'stream',
  spawning: 'dispatch',
  council: 'dispatch',
  deep_focus: 'stream',
  error: 'persist',
  happy: 'persist',
  dreaming: 'observer',
};

/** Voice UI state → the voice stage it means. */
const VOICE_TO_NODE: Record<string, string> = {
  idle: 'mic',
  listening: 'vad',
  speaking_in: 'vad',
  transcribing: 'stt',
  thinking: 'turn',
  speaking: 'tts',
};

/** Tools that belong to a flow's node, so a fired tool lights its stage. */
const TOOL_TO_NODE: Record<string, { flow: string; node: string }> = {
  memory_search: { flow: 'memory', node: 'query' },
  memory_save: { flow: 'memory', node: 'block' },
  delegate_coder: { flow: 'delegation', node: 'seat' },
  delegate_auditor: { flow: 'delegation', node: 'seat' },
};

export interface FlowActivity {
  /** Node ids lit for this flow. */
  active: Set<string>;
  /** True while the newest signal is still flashing. */
  flashing: boolean;
}

export function useFlowActivity(flowId: string): FlowActivity {
  const entityState = useEntityStore((s) => s.state);
  const lastTool = useToolActivityStore((s) => s.lastTool);
  const firedAt = useToolActivityStore((s) => s.firedAt);
  const voiceState = useVoiceStore((s) => s.state);

  const [flashing, setFlashing] = useState(false);
  const lastFired = useRef(0);

  useEffect(() => {
    if (!firedAt || firedAt === lastFired.current) return;
    lastFired.current = firedAt;
    setFlashing(true);
    const timer = window.setTimeout(() => setFlashing(false), FLASH_MS);
    return () => window.clearTimeout(timer);
  }, [firedAt]);

  const active = new Set<string>();

  if (flowId === 'turn') {
    active.add(ENTITY_TO_NODE[entityState]);
    // A tool in flight means the loop under `stream` is where the turn is.
    if (lastTool && Date.now() - firedAt < 4000) {
      active.add('dispatch');
      active.add('permission');
    }
  }

  if (flowId === 'voice') {
    const node = VOICE_TO_NODE[voiceState];
    if (node) active.add(node);
  }

  if (lastTool && Date.now() - firedAt < 4000) {
    const hit = TOOL_TO_NODE[lastTool];
    if (hit && hit.flow === flowId) active.add(hit.node);
  }

  return { active, flashing };
}
