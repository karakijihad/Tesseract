import type { EntityState } from '../../../lib/types';

export type SynapseNodeKind = 'stage' | 'decision';

export interface SynapseNode {
  id: string;
  kind: SynapseNodeKind;
  label: string;
  dataPhase: string;
  depth: 0 | 1 | 2;
  source: string;
}

// 9 real kernel steps traced from tesseract/brain/chat.py + tesseract/brain/tools.py.
// Order matches execution flow; depth drives indentation (.d1/.d2) for the
// .syn-flow rail. Phase 15 (2026-04-25) re-pointed the depth-2 nodes from
// the deleted `kernel/tools/executor.py` to the live `brain/tools.py:execute_tool`.
export const SYNAPSE_NODES: SynapseNode[] = [
  { id: 'user-input',         kind: 'stage',    label: 'User Input',      dataPhase: 'user-input',         depth: 0, source: 'chat.py:132' },
  { id: 'model-select',       kind: 'stage',    label: 'Model Select',    dataPhase: 'model-select',       depth: 0, source: 'chat.py:137' },
  { id: 'chat-session-loop',  kind: 'stage',    label: 'Chat Loop',       dataPhase: 'chat-session-loop',  depth: 0, source: 'chat.py:148' },
  { id: 'stream-text',        kind: 'stage',    label: 'Stream Text',     dataPhase: 'stream-text',        depth: 1, source: 'chat.py:164' },
  { id: 'stream-stop',        kind: 'decision', label: 'Stream Stop',     dataPhase: 'stream-stop',        depth: 1, source: 'chat.py:192' },
  { id: 'permission-check',   kind: 'decision', label: 'Permission',      dataPhase: 'permission-check',   depth: 2, source: 'tools.py:99' },
  { id: 'tool-dispatch',      kind: 'stage',    label: 'Tool Dispatch',   dataPhase: 'tool-dispatch',      depth: 2, source: 'tools.py:65' },
  { id: 'tool-result-append', kind: 'stage',    label: 'Tool Result',     dataPhase: 'tool-result-append', depth: 2, source: 'chat.py:299' },
  { id: 'observer-notify',    kind: 'stage',    label: 'Observer Notify', dataPhase: 'observer-notify',    depth: 0, source: 'chat.py:209' },
];

export const ENTITY_TO_NODE: Record<EntityState, string> = {
  idle: 'stream-stop',
  listening: 'user-input',
  thinking: 'chat-session-loop',
  speaking: 'stream-text',
  spawning: 'tool-dispatch',
  council: 'tool-dispatch',
  deep_focus: 'chat-session-loop',
  error: 'stream-stop',
  happy: 'stream-stop',
  dreaming: 'observer-notify',
};

// Tool groupings rendered below the main flow. Name strings match the
// canonical tool names returned by Tool.name — see tesseract/kernel/tools/*.
// Keep these in sync when new tools land in the registry.
export interface ToolGroupEntry {
  name: string;
  label: string;
}

export interface ToolGroup {
  id: string;
  label: string;
  tools: ToolGroupEntry[];
  // When true, render the group as a single node (highlight if any child
  // tool fires). Used for File + Web where Pulse already surfaces the
  // specific tool name so repeating it under Synapse is noise.
  collapsed?: boolean;
}

export const TOOL_GROUPS: ToolGroup[] = [
  { id: 'memory', label: 'Memory', tools: [
    { name: 'memory_save',    label: 'save' },
    { name: 'memory_update',  label: 'update' },
    { name: 'memory_forget',  label: 'forget' },
    { name: 'memory_search',  label: 'search' },
  ]},
  { id: 'vault', label: 'Vault', tools: [
    { name: 'vault_query',    label: 'query' },
    { name: 'vault_search',   label: 'search' },
    { name: 'vault_ingest',   label: 'ingest' },
  ]},
  { id: 'file', label: 'File', collapsed: true, tools: [
    { name: 'file_read',      label: 'read' },
    { name: 'file_write',     label: 'write' },
    { name: 'glob',           label: 'glob' },
    { name: 'grep',           label: 'grep' },
    { name: 'pdf_read',       label: 'pdf' },
  ]},
  { id: 'delegate', label: 'Delegate', tools: [
    { name: 'delegate_claude', label: 'claude' },
    { name: 'delegate_codex',  label: 'codex' },
  ]},
  { id: 'agents', label: 'Agents', tools: [
    { name: 'invoke_agent',    label: 'invoke' },
    { name: 'agent_create',    label: 'create' },
  ]},
  { id: 'web', label: 'Web', collapsed: true, tools: [
    { name: 'web_search',      label: 'brave' },
    { name: 'tavily_search',   label: 'tavily' },
    { name: 'tavily_extract',  label: 'extract' },
    { name: 'context7_lookup', label: 'ctx7' },
  ]},
  { id: 'shell', label: 'Shell', tools: [
    { name: 'bash',            label: 'bash' },
    { name: 'set_mood',        label: 'mood' },
  ]},
];
