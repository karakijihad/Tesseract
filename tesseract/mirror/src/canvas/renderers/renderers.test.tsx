// Y-2 — renderer registry + reference-renderer smoke tests. Rendered via
// renderToStaticMarkup (no effects) so the registry resolution + static
// markup are exercised without a live browser (the drag/persist path is the
// Playwright spec's job).

import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';

import type { SurfaceDescriptor } from '../protocol/types';
import { CodeRenderer } from './CodeRenderer';
import { FileRenderer } from './FileRenderer';
import { FolderRenderer } from './FolderRenderer';
import { HtmlRenderer } from './HtmlRenderer';
import { ImageRenderer } from './ImageRenderer';
import { JsonDumpRenderer } from './JsonDumpRenderer';
import { MarkdownRenderer } from './MarkdownRenderer';
import { WebViewRenderer } from './WebViewRenderer';
import { getRenderer, RENDERERS } from './';

function desc(over: Partial<SurfaceDescriptor>): SurfaceDescriptor {
  return {
    schema_version: 1,
    id: 'x',
    type: 'folder',
    view: 'tars',
    position: { x: 0, y: 0 },
    size: { w: 400, h: 300 },
    created_at_utc: '2026-06-03T00:00:00Z',
    updated_at_utc: '2026-06-03T00:00:00Z',
    ...over,
  };
}

const noop = () => undefined;

describe('renderer registry', () => {
  it('maps the 8 reference types', () => {
    expect(Object.keys(RENDERERS)).toEqual(
      expect.arrayContaining(['folder', 'file', 'webview', 'terminal', 'code', 'markdown', 'html', 'json']),
    );
  });

  it('falls back to JsonDumpRenderer for an unknown type', () => {
    expect(getRenderer('totally-unknown')).toBe(JsonDumpRenderer);
  });

  it('resolves a known type to its renderer', () => {
    expect(getRenderer('folder')).toBe(FolderRenderer);
  });
});

describe('reference renderers render without crashing', () => {
  it('FolderRenderer lists entries', () => {
    const html = renderToStaticMarkup(
      <FolderRenderer descriptor={desc({ type: 'folder', props: { root: '/r', entries: ['a.txt', { name: 'sub', kind: 'dir' }] } })} dispatch={noop} />,
    );
    expect(html).toContain('/r');
    expect(html).toContain('a.txt');
    expect(html).toContain('sub');
  });

  it('FileRenderer shows text', () => {
    const html = renderToStaticMarkup(
      <FileRenderer descriptor={desc({ type: 'file', props: { text: 'hello world' } })} dispatch={noop} />,
    );
    expect(html).toContain('hello world');
  });

  it('CodeRenderer highlights code', () => {
    const html = renderToStaticMarkup(
      <CodeRenderer descriptor={desc({ type: 'code', props: { text: 'const x = 1;', language: 'javascript' } })} dispatch={noop} />,
    );
    expect(html).toContain('surface-code');
    expect(html).toContain('x');
  });

  it('MarkdownRenderer renders markdown', () => {
    const html = renderToStaticMarkup(
      <MarkdownRenderer descriptor={desc({ type: 'markdown', props: { text: '# Title' } })} dispatch={noop} />,
    );
    expect(html).toContain('Title');
  });

  it('WebViewRenderer frames an allowlisted media embed with the media-capable sandbox', () => {
    const html = renderToStaticMarkup(
      <WebViewRenderer descriptor={desc({ type: 'webview', props: { url: 'https://www.youtube.com/embed/dQw4w9WgXcQ' } })} dispatch={noop} />,
    );
    expect(html).toContain('iframe');
    expect(html).toContain('https://www.youtube.com/embed/dQw4w9WgXcQ');
    // Allowlisted embeds get same-origin + the media feature policy so players boot.
    expect(html).toContain('allow-same-origin');
    expect(html).toContain('encrypted-media');
    expect(html).not.toContain('no-referrer');
  });

  it('WebViewRenderer rewrites a youtube watch url to the trusted /embed/ endpoint', () => {
    const html = renderToStaticMarkup(
      <WebViewRenderer descriptor={desc({ type: 'webview', props: { url: 'https://www.youtube.com/watch?v=dQw4w9WgXcQ' } })} dispatch={noop} />,
    );
    expect(html).toContain('https://www.youtube.com/embed/dQw4w9WgXcQ');
    expect(html).toContain('allow-same-origin');
  });

  it('WebViewRenderer rewrites a youtu.be short link to the trusted /embed/ endpoint', () => {
    const html = renderToStaticMarkup(
      <WebViewRenderer descriptor={desc({ type: 'webview', props: { url: 'https://youtu.be/dQw4w9WgXcQ' } })} dispatch={noop} />,
    );
    expect(html).toContain('https://www.youtube.com/embed/dQw4w9WgXcQ');
    expect(html).toContain('allow-same-origin');
  });

  it('WebViewRenderer keeps the strict sandbox for a watch url with a malformed video id', () => {
    // The 11-char ID gate must reject anything that is not a bare video id.
    const html = renderToStaticMarkup(
      <WebViewRenderer descriptor={desc({ type: 'webview', props: { url: 'https://www.youtube.com/watch?v=../redirect' } })} dispatch={noop} />,
    );
    expect(html).not.toContain('allow-same-origin');
  });

  it('WebViewRenderer keeps the strict sandbox for a non-allowlisted cross-origin url', () => {
    // A page that is cross-origin at load could still 302 to the Mirror's own
    // origin (sandbox flags survive navigation), so it must NOT get same-origin.
    const html = renderToStaticMarkup(
      <WebViewRenderer descriptor={desc({ type: 'webview', props: { url: 'https://example.com/watch?v=x' } })} dispatch={noop} />,
    );
    expect(html).toContain('iframe');
    expect(html).not.toContain('allow-same-origin');
  });

  it('WebViewRenderer does not treat a youtube open-redirect path as a media embed', () => {
    // Only the /embed/ path is trusted; youtube.com/redirect (an open redirector)
    // must fall through to the strict sandbox.
    const html = renderToStaticMarkup(
      <WebViewRenderer descriptor={desc({ type: 'webview', props: { url: 'https://www.youtube.com/redirect?q=http://localhost/x' } })} dispatch={noop} />,
    );
    expect(html).not.toContain('allow-same-origin');
  });

  it('WebViewRenderer rejects a userinfo-spoofed host', () => {
    // https://www.youtube.com@evil.com/embed/ — host is evil.com, not youtube.
    const html = renderToStaticMarkup(
      <WebViewRenderer descriptor={desc({ type: 'webview', props: { url: 'https://www.youtube.com@evil.com/embed/x' } })} dispatch={noop} />,
    );
    expect(html).not.toContain('allow-same-origin');
  });

  it('WebViewRenderer rejects a non-http(s) scheme (javascript:)', () => {
    const html = renderToStaticMarkup(
      <WebViewRenderer descriptor={desc({ type: 'webview', props: { url: 'javascript:alert(1)' } })} dispatch={noop} />,
    );
    expect(html).not.toContain('iframe');
    expect(html).toContain('no url');
  });

  it('HtmlRenderer sandboxes html', () => {
    const html = renderToStaticMarkup(
      <HtmlRenderer descriptor={desc({ type: 'html', props: { html: '<b>hi</b>' } })} dispatch={noop} />,
    );
    expect(html).toContain('sandbox');
  });

  it('HtmlRenderer accepts props.text/content/body as the markup source', () => {
    for (const key of ['text', 'content', 'body']) {
      const html = renderToStaticMarkup(
        <HtmlRenderer descriptor={desc({ type: 'html', props: { [key]: '<b>via-alias</b>' } })} dispatch={noop} />,
      );
      expect(html).toContain('via-alias');
    }
  });

  it('HtmlRenderer injects the in-memory storage shim', () => {
    const html = renderToStaticMarkup(
      <HtmlRenderer descriptor={desc({ type: 'html', props: { html: '<b>hi</b>' } })} dispatch={noop} />,
    );
    expect(html).toContain('memoryStorage');
  });

  it('HtmlRenderer keeps a leading doctype first so surfaces stay in standards mode', () => {
    const html = renderToStaticMarkup(
      <HtmlRenderer
        descriptor={desc({ type: 'html', props: { html: '<!doctype html><body><b>hi</b></body>' } })}
        dispatch={noop}
      />,
    );
    // The doctype must precede the injected shim <script>; otherwise the parser
    // discards it and the surface renders in quirks mode.
    const doctypeAt = html.indexOf('!doctype');
    const shimAt = html.indexOf('memoryStorage');
    expect(doctypeAt).toBeGreaterThanOrEqual(0);
    expect(shimAt).toBeGreaterThanOrEqual(0);
    expect(doctypeAt).toBeLessThan(shimAt);
  });

  it('registry maps iframe to WebViewRenderer', () => {
    expect(getRenderer('iframe')).toBe(WebViewRenderer);
  });

  it('ImageRenderer renders props.url as an img', () => {
    const html = renderToStaticMarkup(
      <ImageRenderer descriptor={desc({ type: 'image', props: { url: '/api/downloads/chat/a/b/flux.jpg' } })} dispatch={noop} />,
    );
    expect(html).toContain('<img');
    expect(html).toContain('flux.jpg');
  });

  it('ImageRenderer handles a missing source without crashing', () => {
    const html = renderToStaticMarkup(
      <ImageRenderer descriptor={desc({ type: 'image', props: {} })} dispatch={noop} />,
    );
    expect(html).toContain('no image');
  });

  it('registry maps image to ImageRenderer', () => {
    expect(getRenderer('image')).toBe(ImageRenderer);
  });

  it('JsonDumpRenderer badges an unknown type', () => {
    const html = renderToStaticMarkup(
      <JsonDumpRenderer descriptor={desc({ type: 'mystery', props: { a: 1 } })} dispatch={noop} />,
    );
    expect(html).toContain('unknown type: mystery');
  });
});
