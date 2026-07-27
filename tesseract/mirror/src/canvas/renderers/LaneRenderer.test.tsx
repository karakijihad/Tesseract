// CV-1 — LaneRenderer + RoutingRenderer + registry smoke (SSR, no effects).

import { afterEach, describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';

import type { SurfaceDescriptor } from '../protocol/types';
import { LaneRenderer, LaneEventRow } from './LaneRenderer';
import { RoutingRenderer } from './RoutingRenderer';
import { getRenderer } from './';
import { useLanesStore } from '../../stores/lanes';

function desc(over: Partial<SurfaceDescriptor>): SurfaceDescriptor {
  return {
    schema_version: 1,
    id: 'x',
    type: 'lane',
    view: 'tars',
    position: { x: 0, y: 0 },
    size: { w: 460, h: 420 },
    created_at_utc: '2026-06-03T00:00:00Z',
    updated_at_utc: '2026-06-03T00:00:00Z',
    ...over,
  };
}

const noop = () => undefined;

afterEach(() => useLanesStore.setState({ byLane: {} }));

describe('LaneRenderer', () => {
  it('is registered for the lane type', () => {
    expect(getRenderer('lane')).toBe(LaneRenderer);
  });

  it('renders descriptor header (name + model) for a bound lane', () => {
    // zustand v5 reads INITIAL state under SSR, so store-driven content
    // (events/status/reattach) is covered by the live e2e; here we lock the
    // descriptor-driven header chrome.
    const html = renderToStaticMarkup(
      <LaneRenderer
        descriptor={desc({ props: { lane_id: 'lane-c', name: 'coder/claude', kind: 'claude', model: 'claude-sonnet-4-6' } })}
        dispatch={noop}
      />,
    );
    expect(html).toContain('coder/claude');
    expect(html).toContain('claude-sonnet-4-6');
    expect(html).toContain('lane-card__dot--claude');
    expect(html).toContain('Message coder/claude');
  });

  it('renders the empty state when no lane is bound', () => {
    const html = renderToStaticMarkup(<LaneRenderer descriptor={desc({ props: {} })} dispatch={noop} />);
    expect(html).toContain('no lane bound');
  });
});

describe('LaneEventRow — typed event fidelity', () => {
  const cases: Array<[string, Record<string, unknown>, string | null]> = [
    ['assistant_text', { text: 'prose here' }, 'prose here'],
    ['tool_use', { name: 'file_read', input: { path: 'a.py' } }, 'file_read'],
    ['tool_result', { output: 'done' }, 'done'],
    ['permission_request', { tool: 'bash' }, 'ASK'],
    ['turn_ended', {}, 'turn complete'],
    ['error', { message: 'boom' }, 'boom'],
    ['closed', {}, 'lane closed'],
    ['status_change', {}, null], // chrome-only → no row
    ['turn_started', {}, null],
  ];
  it.each(cases)('renders %s distinctly', (kind, payload, expected) => {
    const html = renderToStaticMarkup(<LaneEventRow event={{ kind, payload }} />);
    if (expected === null) {
      expect(html).toBe('');
    } else {
      expect(html).toContain(expected);
    }
  });
});

describe('RoutingRenderer', () => {
  it('lists the trio lanes', () => {
    const html = renderToStaticMarkup(
      <RoutingRenderer
        descriptor={desc({ type: 'trio-routing', props: { lanes: [{ name: 'coder/claude', lane_id: 'l1', kind: 'claude' }, { name: 'auditor/codex', lane_id: 'l2', kind: 'codex' }] } })}
        dispatch={noop}
      />,
    );
    expect(html).toContain('TARS');
    expect(html).toContain('coder/claude');
    expect(html).toContain('auditor/codex');
  });
});
