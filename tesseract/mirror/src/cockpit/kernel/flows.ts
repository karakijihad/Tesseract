// The kernel manifest: what the panel draws, as data.
//
// Read from the runtime. `GET /api/cockpit/kernel-manifest` builds it against
// the live tool registry and the config tree the operator owns, so the rail
// shows the seats `roles.yaml` names NOW.
//
// It was a static import of a JSON file a build script had written, and that
// is the defect this replaced: CI checked the committed copy against a dev
// checkout, which kept this repo honest and did nothing for an operator who
// edited a seat in an installed app. They changed it, the rail went on drawing
// the old one, and nothing said so. There is no committed copy to fall back
// to on purpose: a fallback is the same stale picture, shown for longer.
//
// This file is the manifest's TYPES, its store, and its lookup. Editing the
// panel's content means editing `tesseract/cockpit/kernel_manifest.py`.
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

import { create } from 'zustand';

import { ApiError, fetchKernelManifest } from '../../lib/api';

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

/** Nothing until the runtime answers. An empty rail says "reading", which is
 *  true; a seeded one would say the app's build-time guess, which is what this
 *  replaced. */
/** How long to wait before each retry of a failure the route called
 *  retryable. Three tries over about seven seconds, which covers a registry
 *  still building without holding an error off the screen if it never will. */
const RETRIES = [500, 2000, 4500];

interface KernelManifestState {
  flows: Flow[];
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
  load: (attempt?: number) => Promise<void>;
}

export const useKernelManifest = create<KernelManifestState>((set) => ({
  flows: [],
  status: 'idle',
  error: null,
  load: async (attempt = 0) => {
    set({ status: 'loading', error: null });
    try {
      const body = await fetchKernelManifest();
      set({ flows: body.flows ?? [], status: 'ready', error: null });
    } catch (err) {
      // The registry is built by a boot stage, so a panel opened early gets a
      // 503 that WILL come good. The route says which kind it is rather than
      // leaving the rail to guess: `retry` false means waiting changes
      // nothing. Without this the store latched on the first failure and the
      // rail stayed empty until the page was reloaded.
      const payload = err instanceof ApiError ? err.payload : undefined;
      const worthRetrying = payload?.retry !== false && attempt < RETRIES.length;
      if (worthRetrying) {
        setTimeout(() => void useKernelManifest.getState().load(attempt + 1), RETRIES[attempt]);
        return;
      }
      // The rail draws nothing and says why. Holding the last picture would be
      // the stale-manifest defect again, arrived at from the other direction.
      set({
        flows: [],
        status: 'error',
        error: err instanceof Error ? err.message : String(err),
      });
    }
  },
}));

// No `flowById` here. `useFlowActivity` reads the store through a selector,
// which is what keeps it subscribed; a getState() lookup beside it would read
// the same data without subscribing, and the first component to reach for the
// shorter name would silently stop re-rendering.
