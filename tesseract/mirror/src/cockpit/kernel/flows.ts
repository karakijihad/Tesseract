// The kernel manifest: what the panel draws, as data.
//
// Vertical by design. The rail is 280px wide, so a left-to-right schematic
// either scales its labels to 7px or scrolls sideways forever — the stacked
// rail the panel has always used is the form that fits, and the one that
// reads at a glance while a turn is running.
//
// This file is the SHAPE a generator will emit. Until
// `generate_kernel_manifest.py` lands, these node lists are traced by hand
// from the modules named in `source` — which is exactly the drift
// `synapse-nodes.ts` suffered, so the generator is the next step and not an
// optional one.

export type NodeKind = 'stage' | 'gate' | 'store' | 'seat';
export type Tone = 'default' | 'accent' | 'ok' | 'warn' | 'bad' | 'info';

export interface FlowNode {
  id: string;
  label: string;
  /** Second line — what it decides, or what it produced. */
  sub?: string;
  kind: NodeKind;
  /** Nesting under the stage above: the tool loop sits under stream. */
  depth?: 0 | 1 | 2;
  tone?: Tone;
  /** Live signal this node lights on. Consumed by the activity hook. */
  signal?: string;
}

/**
 * A branch or return pinned to the rail after a node — a loop, an exit, a
 * path that crosses turns. The vertical form has no room for arrows, and a
 * one-line note carries the same claim.
 */
export interface FlowNote {
  after: string;
  text: string;
  tone?: Tone;
}

export interface Flow {
  id: string;
  label: string;
  /** What starts this flow — shown instead of a decorative number. */
  trigger: string;
  blurb: string;
  source: string;
  nodes: FlowNode[];
  notes?: FlowNote[];
}

export const FLOWS: Flow[] = [
  {
    id: 'turn',
    label: 'Turn',
    trigger: 'an operator message, typed or spoken',
    blurb:
      'Everything above stream is assembly. The tool loop repeats within a turn; the observer crosses turns.',
    source: 'brain/chat.py::send · brain/tools.py::execute_tool · brain/auto_recall.py',
    nodes: [
      { id: 'input', label: 'operator turn', kind: 'stage', signal: 'turn.start' },
      { id: 'preflight', label: 'cost preflight', sub: 'blocks or asks', kind: 'gate', tone: 'warn', signal: 'cost.preflight' },
      { id: 'drains', label: 'drains', sub: 'comments · spawns', kind: 'stage', signal: 'turn.drains' },
      { id: 'recall', label: 'auto-recall', sub: 'top-k memories', kind: 'stage', signal: 'memory.recall' },
      { id: 'prompt', label: 'prompt assembled', kind: 'stage', signal: 'turn.prompt' },
      { id: 'stream', label: 'stream', sub: 'text · tool calls', kind: 'stage', tone: 'accent', signal: 'turn.stream' },
      { id: 'permission', label: 'permission', sub: 'AUTO · ASK · DENY', kind: 'gate', depth: 1, tone: 'warn', signal: 'tool.permission' },
      { id: 'dispatch', label: 'tool dispatch', sub: '118 registered', kind: 'stage', depth: 1, signal: 'tool.dispatch' },
      { id: 'result', label: 'result appended', kind: 'stage', depth: 1, signal: 'tool.result' },
      { id: 'persist', label: 'persist history', kind: 'stage', signal: 'turn.persist' },
      { id: 'observer', label: 'observer', sub: 'memory suggestions', kind: 'store', tone: 'info', signal: 'observer.fire' },
    ],
    notes: [
      { after: 'result', text: '↺ back to stream, up to tool_iteration_cap', tone: 'accent' },
      { after: 'observer', text: '↑ suggestions drain into the next turn', tone: 'info' },
    ],
  },
  {
    id: 'voice',
    label: 'Voice',
    trigger: 'sound crossing the VAD threshold',
    blurb:
      'The gate is fuzzy, not an exact match: leading tokens are scored against the wake phrase by edit distance.',
    source: 'lib/voice/{vad,stt-stream,tts-player}.ts · server/wake_word.py::match_wake_phrase',
    nodes: [
      { id: 'mic', label: 'mic', sub: 'PCM frames', kind: 'stage', signal: 'voice.mic' },
      { id: 'vad', label: 'VAD', sub: 'speech or silence', kind: 'stage', signal: 'voice.vad' },
      { id: 'stt', label: 'STT', sub: 'streaming', kind: 'stage', signal: 'voice.stt' },
      { id: 'gate', label: 'wake gate', sub: 'score ≥ threshold', kind: 'gate', tone: 'warn', signal: 'voice.gate' },
      { id: 'discarded', label: 'voice_discarded', sub: 'never becomes a turn', kind: 'stage', depth: 1, tone: 'bad', signal: 'voice.discarded' },
      { id: 'turn', label: 'the turn', sub: 'voice_final', kind: 'stage', tone: 'accent', signal: 'turn.start' },
      { id: 'tts', label: 'TTS lane', sub: 'per sentence', kind: 'stage', signal: 'voice.tts' },
      { id: 'audio', label: 'audio out', kind: 'stage', signal: 'voice.audio' },
    ],
    notes: [
      { after: 'discarded', text: '↳ below score — the transcript stops here', tone: 'bad' },
      { after: 'tts', text: 'speech starts at the first sentence, not the last', tone: 'accent' },
      { after: 'audio', text: '↑ barge-in — speech during playback cancels it', tone: 'bad' },
    ],
  },
  {
    id: 'memory',
    label: 'Memory',
    trigger: 'every turn, before the model is called',
    blurb:
      'Four stages, each able to answer alone. BM25 and vector search the full index, not the prefiltered set.',
    source: 'memory/retrieval.py::RetrievalPipeline · memory/reranker.py',
    nodes: [
      { id: 'query', label: 'query', kind: 'stage', signal: 'memory.query' },
      { id: 'exact', label: 'exact slug', sub: 'stage 0', kind: 'stage', signal: 'memory.exact' },
      { id: 'prefilter', label: 'prefilter', sub: 'stage A', kind: 'stage', signal: 'memory.prefilter' },
      { id: 'bm25', label: 'BM25 index', sub: 'stage B', kind: 'store', depth: 1, tone: 'info', signal: 'memory.bm25' },
      { id: 'vector', label: 'vector index', sub: 'stage B', kind: 'store', depth: 1, tone: 'info', signal: 'memory.vector' },
      { id: 'rerank', label: 'rerank', sub: 'stage C · optional', kind: 'stage', signal: 'memory.rerank' },
      { id: 'block', label: '[recalled_memories]', sub: 'one turn only', kind: 'stage', tone: 'ok', signal: 'memory.injected' },
    ],
    notes: [
      { after: 'exact', text: '↓ an exact hit skips every stage below', tone: 'ok' },
      { after: 'vector', text: 'both search the FULL index, not the prefiltered set' },
    ],
  },
  {
    id: 'autonomy',
    label: 'Autonomy',
    trigger: 'a scheduled tick — no operator present',
    blurb:
      'Two gates stand between a proposal and any spend: the vetter, then the governor. Rejections are recorded, not deleted.',
    source: 'orchestrator/autonomy/kernel.py · governor.py · prune_ledger.py',
    nodes: [
      { id: 'events', label: 'workspace events', kind: 'store', tone: 'info', signal: 'autonomy.events' },
      { id: 'writes', label: 'memory writes', kind: 'store', tone: 'info', signal: 'autonomy.writes' },
      { id: 'heartbeat', label: 'heartbeat', sub: 'reads since cursor', kind: 'stage', signal: 'autonomy.heartbeat' },
      { id: 'vetter', label: 'vetter', sub: 'LLM verdict', kind: 'gate', tone: 'warn', signal: 'autonomy.vetter' },
      { id: 'prune', label: 'prune ledger', sub: 'reject · merge', kind: 'store', depth: 1, tone: 'bad', signal: 'autonomy.prune' },
      { id: 'agenda', label: 'agenda', sub: 'scored on save', kind: 'stage', signal: 'autonomy.agenda' },
      { id: 'governor', label: 'governor', sub: 'paused? headroom?', kind: 'gate', tone: 'warn', signal: 'autonomy.governor' },
      { id: 'worker', label: 'worker', sub: 'top-K only', kind: 'stage', tone: 'accent', signal: 'autonomy.worker' },
      { id: 'journal', label: 'journal', sub: 'outcome', kind: 'store', tone: 'info', signal: 'autonomy.journal' },
    ],
    notes: [
      { after: 'heartbeat', text: 'no delta → no model bill', tone: 'ok' },
      { after: 'prune', text: '↳ recorded, not deleted', tone: 'bad' },
      { after: 'journal', text: '↑ outcomes are events too — the loop closes' },
    ],
  },
  {
    id: 'delegation',
    label: 'Delegates',
    trigger: 'the assistant calling a delegate tool',
    blurb:
      'A delegate is a seat, not a program — whatever the role ref names fills it. Completion is injected into a later turn, never awaited inline.',
    source: 'roles.yaml::roles.coder / roles.auditor · kernel/tools/delegate_*.py',
    nodes: [
      { id: 'brain', label: 'chat brain', sub: 'tool call', kind: 'stage', signal: 'tool.dispatch' },
      { id: 'gate', label: 'permission', kind: 'gate', tone: 'warn', signal: 'tool.permission' },
      { id: 'seat', label: 'the seat', sub: 'coder · auditor', kind: 'seat', tone: 'accent', signal: 'delegate.spawn' },
      { id: 'lane', label: 'lane surface', sub: 'streams while it runs', kind: 'stage', depth: 1, signal: 'delegate.stream' },
      { id: 'completion', label: 'completion', sub: 'injected next turn', kind: 'stage', tone: 'accent', signal: 'delegate.complete' },
    ],
    notes: [
      { after: 'seat', text: 'filled by whatever the role ref names — cli.* or api.*' },
      { after: 'completion', text: '↑ not awaited inline — the turn ends first', tone: 'accent' },
    ],
  },
];

export function flowById(id: string): Flow | undefined {
  return FLOWS.find((f) => f.id === id);
}
