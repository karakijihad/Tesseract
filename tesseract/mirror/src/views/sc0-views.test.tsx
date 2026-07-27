// SC-0 — the reverted views render their real content standalone (not a
// <CanvasShell> shell). Static-markup smoke: effects don't run, so no REST /
// xterm bootstrap fires; we only assert the whole-view chrome is present.

import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';

import { PulseView } from './PulseView';
import { TerminalView } from './TerminalView';

describe('SC-0 whole-view rendering', () => {
  it('PulseView renders header + filter chips + stream (not a canvas shell)', () => {
    const html = renderToStaticMarkup(<PulseView />);
    expect(html).toContain('pulse-view');
    expect(html).toContain('pulse-header-title');
    expect(html).toContain('pulse-filters');
    expect(html).toContain('Waiting for events…');
    expect(html).not.toContain('canvas-shell');
  });

  it('TerminalView renders the real terminal chrome (not a canvas shell)', () => {
    const html = renderToStaticMarkup(<TerminalView />);
    expect(html).toContain('wt-tabbar');
    expect(html).not.toContain('canvas-shell');
  });
});
