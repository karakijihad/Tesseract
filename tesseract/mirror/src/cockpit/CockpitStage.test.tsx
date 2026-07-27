// SC-1 — the fixed spatial cockpit shell renders its chrome: a permanent
// `data-view="tars"` shell, the perspective center stage with the orb anchor +
// the (empty) panel-host slot, both rails, the top status pill, and the bottom
// command HUD. Static-markup smoke: effects don't run, so no REST/WS bootstrap
// fires. The orb itself is GlobalCanvas (a separate App-root sibling), not part
// of the stage tree. Asserting `not canvas-shell` guards the tldraw demotion
// (spec §4 / SC-1): the center is plain DOM over the orb, not a tldraw canvas.

import { beforeAll, describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';

// CockpitStage pulls the whole BottomHud → HudChatInput → ChatPdfPreview chain,
// which imports pdfjs-dist; its canvas backend needs a `DOMMatrix` the jsdom
// env lacks. The cockpit frame doesn't touch PDF rendering, so stub the module
// (hoisted before the CockpitStage import) to keep the smoke env-clean.
vi.mock('pdfjs-dist', () => ({
  GlobalWorkerOptions: {},
  getDocument: () => ({ promise: Promise.resolve(null) }),
}));

import { CockpitStage } from './CockpitStage';

describe('SC-1 cockpit stage', () => {
  // Render once in beforeAll (not at describe scope) so a render-time throw
  // surfaces as a setup failure rather than a silent collection error.
  let html = '';
  beforeAll(() => {
    html = renderToStaticMarkup(<CockpitStage />);
  });

  it('renders the permanent immersive shell frame', () => {
    expect(html).toContain('cockpit-shell');
    expect(html).toContain('data-view="tars"');
    expect(html).toContain('cockpit-center');
    expect(html).toContain('cockpit-hud');
  });

  it('renders the perspective center stage with orb anchor + panel-host slot', () => {
    expect(html).toContain('cockpit-stage');
    expect(html).toContain('orb-anchor');
    expect(html).toContain('panel-host');
  });

  it('renders the top status pill (rails are now dockable panels, not asides)', () => {
    expect(html).toContain('top-status-hud');
    // SC-3 retired the grid asides — the rails are panels seeded by an effect,
    // which static markup does not run, so the old `.cockpit-left/right` columns
    // must be gone.
    expect(html).not.toContain('cockpit-left');
    expect(html).not.toContain('cockpit-right');
  });

  it('does not host a tldraw canvas in the center (tldraw demoted, spec §4)', () => {
    expect(html).not.toContain('canvas-shell');
  });
});
