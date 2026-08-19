// The kernel manifest: what the panel draws, as data.
//
// Generated. `tesseract/scripts/generate_kernel_manifest.py` walks the live
// tool registry and `roles.yaml` and writes `kernel-manifest.json`; CI runs
// it with `--check` and fails when the committed file no longer matches the
// runtime. This file is now only the manifest's TYPES and its lookup — the
// node lists it used to declare by hand are gone, along with the drift they
// invited. Editing the panel's content means editing the generator.
//
// Vertical by design. The rail is 280px wide, so a left-to-right schematic
// either scales its labels to 7px or scrolls sideways forever — the stacked
// rail the panel has always used is the form that fits, and the one that
// reads at a glance while a turn is running.
//
// Three flows, because voice is not a pipeline of its own: it is how a turn
// gets in and how it comes back out. Autonomy is off this rail entirely —
// it has its own view, and the kernel covers the turn and what the turn
// touches.

import manifest from './kernel-manifest.json';

export type NodeKind = 'stage' | 'gate' | 'store' | 'seat';
export type Tone = 'default' | 'accent' | 'ok' | 'warn' | 'bad' | 'info';

export interface FlowNode {
  id: string;
  label: string;
  /** Second line — what it decides, what it produced, where it returns to. */
  sub?: string;
  kind: NodeKind;
  /**
   * Nesting under the stage above: the tool loop sits under stream. Depth is
   * also what the activity hook reads to decide whether a node is on the
   * spine every run takes (depth 0) or on a branch that has to be visited
   * before it counts as passed.
   */
  depth?: 0 | 1 | 2;
  tone?: Tone;
  /** Live signal this node lights on. Consumed by the activity hook. */
  signal?: string;
  /**
   * Tool names filed under this node, from the live registry. A fire lights
   * the node and adds to the count it carries — the group is the unit,
   * because 118 nodes is a directory and nobody watches a directory.
   */
  tools?: string[];
}

export interface Flow {
  id: string;
  label: string;
  /** The modules the generator traced to build this flow. */
  source: string;
  nodes: FlowNode[];
}

export const FLOWS: Flow[] = manifest.flows as Flow[];

export function flowById(id: string): Flow | undefined {
  return FLOWS.find((f) => f.id === id);
}
