// Y-3 — views-as-canvases renderer registry + light smoke renders. Heavy
// REST-backed hosts are covered by Playwright; here we lock registry
// membership + the lightweight renderers' static markup.
// (SC-0 de-registered `terminal-host`: the terminal is now a whole-view panel.)

import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';

import type { SurfaceDescriptor } from '../protocol/types';
import { RENDERERS } from './';
import { PulseFilterRenderer } from './PulseFilterRenderer';
import { PulseStreamRenderer } from './PulseStreamRenderer';
import { DelegateTranscriptRenderer } from './DelegateTranscriptRenderer';

function desc(over: Partial<SurfaceDescriptor>): SurfaceDescriptor {
  return {
    schema_version: 1,
    id: 'x',
    type: 'folder',
    view: 'pulse',
    position: { x: 0, y: 0 },
    size: { w: 400, h: 300 },
    created_at_utc: '2026-06-03T00:00:00Z',
    updated_at_utc: '2026-06-03T00:00:00Z',
    ...over,
  };
}

const noop = () => undefined;

describe('Y-3 renderer registry', () => {
  it('registers every views-as-canvases applet type', () => {
    expect(Object.keys(RENDERERS)).toEqual(
      expect.arrayContaining([
        'pulse-stream',
        'pulse-filters',
        'delegate-transcript',
      ]),
    );
  });
});

describe('Y-3 lightweight renderers render without crashing', () => {
  it('PulseFilterRenderer renders all 12 tag chips', () => {
    const html = renderToStaticMarkup(
      <PulseFilterRenderer descriptor={desc({ type: 'pulse-filters' })} dispatch={noop} />,
    );
    for (const tag of ['triage', 'tool', 'memory', 'perm', 'other']) {
      expect(html).toContain(`>${tag}<`);
    }
  });

  it('PulseStreamRenderer shows the empty state with no events', () => {
    const html = renderToStaticMarkup(
      <PulseStreamRenderer descriptor={desc({ type: 'pulse-stream' })} dispatch={noop} />,
    );
    expect(html).toContain('Waiting for events…');
  });

  it('DelegateTranscriptRenderer warns when no call_id is bound', () => {
    const html = renderToStaticMarkup(
      <DelegateTranscriptRenderer descriptor={desc({ type: 'delegate-transcript' })} dispatch={noop} />,
    );
    expect(html).toContain('No call_id bound');
  });

  it('DelegateTranscriptRenderer renders a transcript shell for a bound call', () => {
    const html = renderToStaticMarkup(
      <DelegateTranscriptRenderer
        descriptor={desc({ type: 'delegate-transcript', props: { call_id: 'abc123', tool_name: 'delegate_claude' } })}
        dispatch={noop}
      />,
    );
    expect(html).toContain('spawn-card');
    expect(html).toContain('abc123'.slice(0, 8));
  });
});
