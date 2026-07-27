// Session-transcript card smoke (SSR, no effects — the WS opens in useEffect,
// which renderToStaticMarkup never runs).

import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';

import type { SurfaceDescriptor } from '../protocol/types';
import { SessionTranscriptRenderer } from './SessionTranscriptRenderer';
import { getRenderer } from './';

function desc(over: Partial<SurfaceDescriptor>): SurfaceDescriptor {
  return {
    schema_version: 1,
    id: 'x',
    type: 'session-transcript',
    view: 'tars',
    position: { x: 0, y: 0 },
    size: { w: 560, h: 520 },
    created_at_utc: '2026-06-30T00:00:00Z',
    updated_at_utc: '2026-06-30T00:00:00Z',
    ...over,
  };
}

const noop = () => undefined;

describe('SessionTranscriptRenderer', () => {
  it('is registered for the session-transcript type', () => {
    expect(getRenderer('session-transcript')).toBe(SessionTranscriptRenderer);
  });

  it('mounts ControllerMirrorBlock for a bound session', () => {
    const html = renderToStaticMarkup(
      <SessionTranscriptRenderer descriptor={desc({ props: { session_id: 'sess-42' } })} dispatch={noop} />,
    );
    expect(html).toContain('sess-42');
    expect(html).toContain('tars --session sess-42');
  });

  it('renders the empty state when no session is bound', () => {
    const html = renderToStaticMarkup(
      <SessionTranscriptRenderer descriptor={desc({ props: {} })} dispatch={noop} />,
    );
    expect(html).toContain('No session bound');
  });
});
