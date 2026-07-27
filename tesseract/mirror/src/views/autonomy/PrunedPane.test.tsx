// AU-7 Phase 3 — PrunedPane render tests.
//
// Tests the pure `PrunedPaneView` via props, matching JournalPane.test.tsx
// — zustand v5's `useStore` reads `getInitialState()` for the SSR
// snapshot, so `renderToStaticMarkup` against a store-connected component
// ignores `useAutonomyStore.setState(...)`. Props avoid that trap.

import { act } from 'react';
import { afterEach, describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { createRoot } from 'react-dom/client';
import { PrunedPane, PrunedPaneView } from './PrunedPane';
import { useAutonomyStore } from '../../stores/autonomy';
import type { PrunedResponse } from '../../lib/api';

const noop = () => {};

describe('PrunedPaneView', () => {
  it('renders loading state when pruned is null', () => {
    const html = renderToStaticMarkup(
      <PrunedPaneView
        pruned={null}
        prunedStatus="idle"
        mutedSources={new Set()}
        pending={new Set()}
        onMute={noop}
        onRefresh={noop}
      />,
    );
    expect(html).toContain('Loading');
  });

  it('renders error state', () => {
    const html = renderToStaticMarkup(
      <PrunedPaneView
        pruned={null}
        prunedStatus="error"
        mutedSources={new Set()}
        pending={new Set()}
        onMute={noop}
        onRefresh={noop}
      />,
    );
    expect(html).toContain('Failed to load pruned ledger');
  });

  it('renders the counts table + a mute button per source, flagging a hot source', () => {
    const pruned: PrunedResponse = {
      records: [
        {
          item_id: 'ag-1',
          source: 'observer',
          goal: 'check the weather forecast for tomorrow morning briefing',
          stage: 'low_value',
          reason: 'below threshold',
          ts: '2026-05-23T12:00:00+00:00',
        },
      ],
      counts: {
        observer: { malformed: 0, duplicate: 1, low_value: 12, capped: 0 },
      },
    };
    const html = renderToStaticMarkup(
      <PrunedPaneView
        pruned={pruned}
        prunedStatus="idle"
        mutedSources={new Set()}
        pending={new Set()}
        onMute={noop}
        onRefresh={noop}
      />,
    );
    expect(html).toContain('pruned-counts-table');
    expect(html).toContain('observer');
    expect(html).toContain('Mute');
    expect(html).toContain('pruned-table__row--hot');
  });
});

// Regression: the connected wrapper's `governor` selector must return a
// referentially STABLE value when governor.data is null. The old
// `(s) => s.governor.data?.pauses ?? []` handed useSyncExternalStore a fresh
// [] every call, so React spun "Maximum update depth exceeded" on mount and
// blanked the Autonomy view. A client mount (jsdom) runs the passive effects
// where that loop surfaced; SSR did not, which is why it slipped through.
describe('PrunedPane (connected) — getSnapshot stability', () => {
  const g = globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT?: boolean; fetch: typeof fetch };
  let origFetch: typeof fetch;

  afterEach(() => {
    globalThis.fetch = origFetch;
    useAutonomyStore.setState((s) => ({ governor: { ...s.governor, data: null } }));
  });

  it('mounts without an infinite getSnapshot loop when governor.data is null', async () => {
    g.IS_REACT_ACT_ENVIRONMENT = true;
    origFetch = globalThis.fetch;
    // Quiet the mount-time loadPruned() fetch — the store action catches the rejection.
    globalThis.fetch = (() => Promise.reject(new Error('no network in test'))) as typeof fetch;
    useAutonomyStore.setState((s) => ({ governor: { ...s.governor, data: null } }));

    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    try {
      // With the old fresh-[] selector this throws "Maximum update depth
      // exceeded" during the passive-effect flush; with the fix it renders.
      await act(async () => {
        root.render(<PrunedPane />);
      });
      expect(host.textContent).toBeTruthy();
    } finally {
      await act(async () => root.unmount());
      host.remove();
    }
  });
});

describe('PrunedPaneView (muted)', () => {
  it('shows "Muted" for a source already in mutedSources', () => {
    const pruned: PrunedResponse = {
      records: [],
      counts: {
        scheduler: { malformed: 1, duplicate: 0, low_value: 0, capped: 0 },
      },
    };
    const html = renderToStaticMarkup(
      <PrunedPaneView
        pruned={pruned}
        prunedStatus="idle"
        mutedSources={new Set(['scheduler'])}
        pending={new Set()}
        onMute={noop}
        onRefresh={noop}
      />,
    );
    expect(html).toContain('Muted');
  });
});
