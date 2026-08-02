// The four renderers `open` needs to keep a target inside the cockpit.
//
// Rendered via renderToStaticMarkup like `renderers.test.tsx`, so this covers
// static markup and the table parser — the part carrying real logic. The
// effect-driven paths (pdf.js paging, the media onError fallback) need a live
// DOM and belong to the Playwright spec.

import { describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';

import type { SurfaceDescriptor } from '../protocol/types';
import { KNOWN_SURFACE_TYPES } from '../protocol/types';
import { AudioRenderer } from './AudioRenderer';
import { JsonDumpRenderer } from './JsonDumpRenderer';
import { TableRenderer } from './TableRenderer';
import { VideoRenderer } from './VideoRenderer';
import { getRenderer, RENDERERS } from './';

vi.mock('pdfjs-dist', () => ({
  GlobalWorkerOptions: { workerSrc: '' },
  getDocument: () => ({ promise: Promise.reject(new Error('stubbed')) }),
}));
vi.mock('pdfjs-dist/build/pdf.worker.mjs?url', () => ({ default: 'worker' }));

function desc(over: Partial<SurfaceDescriptor>): SurfaceDescriptor {
  return {
    schema_version: 1,
    id: 'x',
    type: 'x',
    view: 'tars',
    position: { x: 0, y: 0 },
    size: { w: 100, h: 100 },
    created_at_utc: '',
    updated_at_utc: '',
    ...over,
  };
}

const dispatch = () => {};

function markup(Renderer: (p: never) => JSX.Element, props: Record<string, unknown>, title?: string) {
  return renderToStaticMarkup(
    // @ts-expect-error — RendererProps is structurally satisfied here.
    <Renderer descriptor={desc({ props, title })} dispatch={dispatch} />,
  );
}

describe('media renderers', () => {
  it('renders a video with controls and metadata-only preload', () => {
    const html = markup(VideoRenderer, { url: '/api/asset?path=a.mp4' });
    expect(html).toContain('<video');
    expect(html).toContain('controls');
    expect(html).toContain('preload="metadata"');
  });

  it('renders audio with controls', () => {
    const html = markup(AudioRenderer, { url: '/api/asset?path=a.mp3' });
    expect(html).toContain('<audio');
    expect(html).toContain('controls');
  });

  it('shows the title as a caption on audio', () => {
    expect(markup(AudioRenderer, { url: '/a.mp3' }, 'take.mp3')).toContain('take.mp3');
  });

  it.each([
    ['video', VideoRenderer],
    ['audio', AudioRenderer],
  ])('%s with no url shows a hint rather than an empty box', (_name, Renderer) => {
    expect(markup(Renderer as never, {})).toContain('surface-media--empty');
  });

  it.each([
    ['video', VideoRenderer],
    ['audio', AudioRenderer],
  ])('%s hint text uses the meta token, never text-dim', (_name, Renderer) => {
    expect(markup(Renderer as never, {})).toContain('t-meta');
  });
});

describe('TableRenderer', () => {
  const cells = (html: string, tag: 'th' | 'td') =>
    // `[\s\S]` not `.` — a quoted cell may contain a newline, and `.` would
    // skip that row entirely. Doubled backslashes: this is a template literal.
    [...html.matchAll(new RegExp(`<${tag}>([\\s\\S]*?)</${tag}>`, 'g'))].map((m) => m[1]);

  it('renders a csv grid', () => {
    const html = markup(TableRenderer, { text: 'a,b\n1,2\n3,4' });
    expect(cells(html, 'th')).toEqual(['a', 'b']);
    expect(cells(html, 'td')).toEqual(['1', '2', '3', '4']);
  });

  it('detects tabs when no delimiter is given', () => {
    expect(cells(markup(TableRenderer, { text: 'a\tb\tc\n1\t2\t3' }), 'th')).toHaveLength(3);
  });

  it('honours an explicit delimiter', () => {
    const html = markup(TableRenderer, { text: 'a\tb\n1\t2', delimiter: '\t' });
    expect(cells(html, 'th')).toEqual(['a', 'b']);
  });

  it('keeps a delimiter that sits inside quotes as data', () => {
    const html = markup(TableRenderer, { text: 'name,note\n"Doe, Jane",hi' });
    expect(cells(html, 'td')).toEqual(['Doe, Jane', 'hi']);
  });

  it('reads a doubled quote as a literal one', () => {
    const html = markup(TableRenderer, { text: 'a\n"say ""hi"""' });
    expect(cells(html, 'td')).toEqual(['say &quot;hi&quot;']);
  });

  it('keeps a newline inside quotes in the same cell', () => {
    const html = markup(TableRenderer, { text: 'a,b\n"one\ntwo",x' });
    expect(cells(html, 'td')).toEqual(['one\ntwo', 'x']);
  });

  it('pads a ragged row rather than dropping it', () => {
    const html = markup(TableRenderer, { text: 'a,b,c\n1,2' });
    expect(cells(html, 'td')).toEqual(['1', '2', '']);
  });

  it('states the cap instead of silently truncating', () => {
    const rows = ['h', ...Array.from({ length: 600 }, (_, i) => String(i))].join('\n');
    expect(markup(TableRenderer, { text: rows })).toContain(
      'showing the first 500 of 600 rows',
    );
  });

  it('shows no cap note when everything fits', () => {
    expect(markup(TableRenderer, { text: 'a\n1\n2' })).not.toContain('showing the first');
  });

  it('shows a hint for empty text', () => {
    expect(markup(TableRenderer, { text: '  ' })).toContain('surface-table--empty');
  });
});

describe('registry', () => {
  it.each(['pdf', 'video', 'audio', 'table'])('%s resolves to a real renderer', (type) => {
    expect(RENDERERS[type]).toBeTruthy();
    expect(getRenderer(type)).not.toBe(JsonDumpRenderer);
  });

  it('every known surface type has a renderer', () => {
    // `terminal-host` was de-registered in SC-0 on purpose — the cockpit hosts
    // the whole TerminalView in a panel, not as a surface card. `json` maps to
    // the dump renderer by design; it is that type's real renderer.
    const exempt = new Set(['terminal-host', 'json']);
    const missing = KNOWN_SURFACE_TYPES.filter(
      (type) => !exempt.has(type) && getRenderer(type) === JsonDumpRenderer,
    );
    expect(missing).toEqual([]);
  });
});
