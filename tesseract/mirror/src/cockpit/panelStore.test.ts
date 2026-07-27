// SC-2 — panel manager store. Open/close/focus/reset semantics, z-ordering,
// keep-mounted-on-close, geometry, and the `view`-state coupling (openPanel /
// focus / close / reset drive useUIStore.view). Pure store — no DOM, no views.

import { beforeEach, describe, expect, it } from 'vitest';

import { usePanelStore } from './panelStore';
import { useUIStore } from '../stores/ui';

const reset = () => {
  usePanelStore.setState({ panels: [], topZ: 10 });
  useUIStore.setState({ view: 'autonomy' });
};

const panel = (kind: string) => usePanelStore.getState().panels.find((p) => p.id === kind);
const view = () => useUIStore.getState().view;

describe('SC-2 panelStore', () => {
  beforeEach(reset);

  it('openPanel creates an open panel and drives view', () => {
    usePanelStore.getState().openPanel('schedule');
    expect(usePanelStore.getState().panels).toHaveLength(1);
    expect(panel('schedule')?.open).toBe(true);
    expect(view()).toBe('schedule');
  });

  it('a second kind opens above the first (z-order) and steals focus', () => {
    const s = usePanelStore.getState();
    s.openPanel('schedule');
    s.openPanel('pulse');
    expect(usePanelStore.getState().panels).toHaveLength(2);
    expect(panel('pulse')!.z).toBeGreaterThan(panel('schedule')!.z);
    expect(view()).toBe('pulse');
  });

  it('re-opening an existing kind reuses the panel and raises it', () => {
    const s = usePanelStore.getState();
    s.openPanel('schedule');
    s.openPanel('pulse');
    s.openPanel('schedule');
    expect(usePanelStore.getState().panels).toHaveLength(2); // no duplicate
    expect(panel('schedule')!.z).toBeGreaterThan(panel('pulse')!.z);
    expect(view()).toBe('schedule');
  });

  it('focus raises a background panel, no-ops on top, and drives view', () => {
    const s = usePanelStore.getState();
    s.openPanel('schedule');
    s.openPanel('pulse');
    const zBefore = panel('pulse')!.z;
    s.focus('pulse'); // already top → no z change
    expect(panel('pulse')!.z).toBe(zBefore);
    expect(view()).toBe('pulse');
    s.focus('schedule'); // background → raised + focused
    expect(panel('schedule')!.z).toBeGreaterThan(panel('pulse')!.z);
    expect(view()).toBe('schedule');
  });

  it('focus stays a no-op even after a higher-z panel was closed (stale z ignored)', () => {
    const s = usePanelStore.getState();
    s.openPanel('schedule');
    s.openPanel('pulse'); // pulse has the highest z
    s.closePanel('pulse'); // pulse closed but keeps its stale (highest) z
    const zBefore = panel('schedule')!.z;
    s.focus('schedule'); // only open panel → must NOT bump despite pulse's stale z
    expect(panel('schedule')!.z).toBe(zBefore);
  });

  it('closePanel hides but keeps the panel mounted, and hands focus to next', () => {
    const s = usePanelStore.getState();
    s.openPanel('schedule');
    s.openPanel('pulse');
    s.closePanel('pulse');
    expect(usePanelStore.getState().panels).toHaveLength(2); // kept (keep-mounted)
    expect(panel('pulse')!.open).toBe(false);
    expect(view()).toBe('schedule'); // focus fell to the remaining open panel
  });

  it('closing a non-topmost panel keeps focus on the remaining top panel', () => {
    const s = usePanelStore.getState();
    s.openPanel('schedule');
    s.openPanel('pulse'); // pulse on top, view = pulse
    s.closePanel('schedule'); // close the background panel
    expect(panel('schedule')!.open).toBe(false);
    expect(panel('pulse')!.open).toBe(true);
    expect(view()).toBe('pulse'); // top panel keeps focus
  });

  it('closing the last open panel parks view on the orb home', () => {
    const s = usePanelStore.getState();
    s.openPanel('schedule');
    s.closePanel('schedule');
    expect(panel('schedule')!.open).toBe(false);
    expect(view()).toBe('tars');
  });

  it('opening the already-active open tab again closes it (back to orb home)', () => {
    const s = usePanelStore.getState();
    s.openPanel('schedule');
    s.openPanel('schedule'); // second click on the active tab
    expect(panel('schedule')!.open).toBe(false);
    expect(view()).toBe('tars');
  });

  it('opening the already-active tab again falls back to the remaining open panel', () => {
    const s = usePanelStore.getState();
    s.openPanel('schedule');
    s.openPanel('pulse'); // pulse active on top
    s.openPanel('pulse'); // second click closes pulse, not schedule
    expect(panel('pulse')!.open).toBe(false);
    expect(panel('schedule')!.open).toBe(true);
    expect(view()).toBe('schedule');
  });

  it('resetAll closes every panel and returns home', () => {
    const s = usePanelStore.getState();
    s.openPanel('schedule');
    s.openPanel('pulse');
    s.resetAll();
    expect(usePanelStore.getState().panels.every((p) => !p.open)).toBe(true);
    expect(view()).toBe('tars');
  });

  it('tars is the orb home — openPanel(tars) opens no panel and resets', () => {
    const s = usePanelStore.getState();
    s.openPanel('schedule');
    s.openPanel('tars');
    expect(panel('tars')).toBeUndefined();
    expect(usePanelStore.getState().panels.every((p) => !p.open)).toBe(true);
    expect(view()).toBe('tars');
  });

  it('ensureRails seeds Kernel(left) + Lifeline(right) docked + open, idempotently', () => {
    const s = usePanelStore.getState();
    s.ensureRails();
    s.ensureRails(); // idempotent — no duplicates
    const kernel = panel('kernel');
    const lifeline = panel('lifeline');
    expect(usePanelStore.getState().panels.filter((p) => p.id === 'kernel')).toHaveLength(1);
    expect(kernel).toMatchObject({ dock: 'left', open: true });
    expect(lifeline).toMatchObject({ dock: 'right', open: true });
  });

  it('toggleRail hides/shows a rail without touching view', () => {
    const s = usePanelStore.getState();
    s.ensureRails();
    s.openPanel('schedule'); // view = schedule
    s.toggleRail('kernel');
    expect(panel('kernel')!.open).toBe(false);
    expect(view()).toBe('schedule'); // rail toggle must not change view
    s.toggleRail('kernel');
    expect(panel('kernel')!.open).toBe(true);
  });

  it('closing a rail via × leaves view untouched', () => {
    const s = usePanelStore.getState();
    s.ensureRails();
    s.openPanel('pulse');
    s.closePanel('lifeline');
    expect(panel('lifeline')!.open).toBe(false);
    expect(view()).toBe('pulse'); // not reset to tars
  });

  it('dragging a docked rail undocks it', () => {
    const s = usePanelStore.getState();
    s.ensureRails();
    expect(panel('kernel')!.dock).toBe('left');
    s.move('kernel', 400, 200);
    expect(panel('kernel')).toMatchObject({ x: 400, y: 200, dock: null });
  });

  it('focusing a DOCKED rail does not raise it above view panels', () => {
    const s = usePanelStore.getState();
    s.ensureRails();
    s.openPanel('schedule'); // view panel at z=11, kernel docked at z=4
    const railZ = panel('kernel')!.z;
    s.focus('kernel'); // docked rail → must NOT bump
    expect(panel('kernel')!.z).toBe(railZ);
    expect(panel('schedule')!.z).toBeGreaterThan(panel('kernel')!.z);
    expect(view()).toBe('schedule'); // rail focus doesn't change view
  });

  it('focusing an UNDOCKED (floating) rail raises it like any panel', () => {
    const s = usePanelStore.getState();
    s.ensureRails();
    s.openPanel('schedule');
    s.move('kernel', 300, 200); // drag off → undocked
    s.focus('kernel');
    expect(panel('kernel')!.z).toBeGreaterThan(panel('schedule')!.z);
  });

  it('resetAll closes view panels AND re-docks + re-shows the rails', () => {
    const s = usePanelStore.getState();
    s.ensureRails();
    s.openPanel('schedule');
    s.toggleRail('kernel'); // hide kernel
    s.move('lifeline', 500, 300); // undock lifeline
    s.resetAll();
    expect(panel('schedule')!.open).toBe(false); // view closed
    expect(panel('kernel')).toMatchObject({ open: true, dock: 'left', placed: false });
    expect(panel('lifeline')).toMatchObject({ open: true, dock: 'right', placed: false });
    expect(view()).toBe('tars');
  });

  it('togglePin marks a panel pinned; resetAll keeps pinned, closes unpinned', () => {
    const s = usePanelStore.getState();
    s.ensureRails();
    s.openPanel('schedule');
    s.openPanel('pulse');
    s.togglePin('schedule');
    expect(panel('schedule')!.pinned).toBe(true);
    s.resetAll();
    expect(panel('schedule')!.open).toBe(true); // pinned survives reset
    expect(panel('pulse')!.open).toBe(false); // unpinned closed
    expect(panel('kernel')!.open).toBe(true); // rails re-shown
    expect(view()).toBe('tars');
  });

  it('a pinned + maximized panel survives reset: open, un-maximized', () => {
    const s = usePanelStore.getState();
    s.openPanel('schedule');
    s.togglePin('schedule');
    s.toggleMaximize('schedule');
    s.resetAll();
    expect(panel('schedule')).toMatchObject({ open: true, maximized: false, pinned: true });
  });

  it('toggleMaximize flips the flag; resetAll un-maximizes', () => {
    const s = usePanelStore.getState();
    s.openPanel('settings');
    s.toggleMaximize('settings');
    expect(panel('settings')!.maximized).toBe(true);
    s.toggleMaximize('settings');
    expect(panel('settings')!.maximized).toBe(false);
    s.toggleMaximize('settings');
    s.resetAll();
    // pinned? no → closed AND un-maximized
    expect(panel('settings')).toMatchObject({ open: false, maximized: false });
  });

  const saved = (over: Record<string, unknown> = {}) => ({
    kind: 'pulse' as const,
    x: 50,
    y: 60,
    w: 700,
    h: 500,
    pinned: true,
    minimized: false,
    maximized: false,
    ...over,
  });

  it('hydratePanels re-opens saved panels at their geometry + state, placed', () => {
    const s = usePanelStore.getState();
    s.hydratePanels([saved()]);
    expect(panel('pulse')).toMatchObject({
      open: true,
      pinned: true,
      minimized: false,
      maximized: false,
      placed: true,
      x: 50,
      y: 60,
      w: 700,
      h: 500,
    });
    // idempotent: a kind already present is not duplicated
    s.hydratePanels([saved({ x: 1, y: 1, w: 1, h: 1 })]);
    expect(usePanelStore.getState().panels.filter((p) => p.id === 'pulse')).toHaveLength(1);
    expect(panel('pulse')!.x).toBe(50); // original kept
  });

  it('hydratePanels restores a minimized panel', () => {
    usePanelStore.getState().hydratePanels([saved({ pinned: false, minimized: true })]);
    expect(panel('pulse')).toMatchObject({ open: true, minimized: true });
  });

  it('toggleMinimize collapses then restores-to-front + re-activates view', () => {
    const s = usePanelStore.getState();
    s.openPanel('schedule');
    s.openPanel('pulse'); // pulse on top, view=pulse
    s.toggleMinimize('pulse');
    expect(panel('pulse')!.minimized).toBe(true);
    expect(panel('pulse')!.open).toBe(true); // kept mounted
    // restore: un-minimized, raised above schedule, view back to pulse
    s.toggleMinimize('pulse');
    expect(panel('pulse')!.minimized).toBe(false);
    expect(panel('pulse')!.z).toBeGreaterThan(panel('schedule')!.z);
    expect(view()).toBe('pulse');
  });

  it('minimizing the focused panel hands view to the next open panel', () => {
    const s = usePanelStore.getState();
    s.openPanel('schedule');
    s.openPanel('pulse'); // pulse on top, view=pulse
    s.toggleMinimize('pulse');
    expect(view()).toBe('schedule');
    s.toggleMinimize('schedule');
    expect(view()).toBe('tars'); // nothing left on stage
  });

  it('closing the active panel never hands view to a minimized panel', () => {
    const s = usePanelStore.getState();
    s.openPanel('schedule');
    s.toggleMinimize('schedule');
    s.openPanel('pulse'); // view=pulse; schedule open-but-minimized
    s.closePanel('pulse');
    expect(view()).toBe('tars'); // not 'schedule' — it is off-stage
  });

  it('openPanel on a minimized panel un-minimizes it (tab click brings it back)', () => {
    const s = usePanelStore.getState();
    s.openPanel('schedule');
    s.toggleMinimize('schedule');
    expect(panel('schedule')!.minimized).toBe(true);
    s.openPanel('schedule'); // tab click again
    expect(panel('schedule')).toMatchObject({ open: true, minimized: false });
  });

  it('resetAll clears minimized on kept panels', () => {
    const s = usePanelStore.getState();
    s.openPanel('settings');
    s.togglePin('settings');
    s.toggleMinimize('settings');
    s.resetAll();
    expect(panel('settings')).toMatchObject({ open: true, minimized: false });
  });

  it('place / move / resize update geometry', () => {
    const s = usePanelStore.getState();
    s.openPanel('settings');
    s.place('settings', { x: 100, y: 80, w: 800, h: 560 });
    expect(panel('settings')).toMatchObject({ x: 100, y: 80, w: 800, h: 560, placed: true });
    s.move('settings', 120, 90);
    expect(panel('settings')).toMatchObject({ x: 120, y: 90 });
    s.resize('settings', 700, 500);
    expect(panel('settings')).toMatchObject({ w: 700, h: 500 });
  });

  it('hydrateRails restores a hidden / pinned docked rail; ensureRails skips it', () => {
    const s = usePanelStore.getState();
    s.hydrateRails([
      { kind: 'kernel', open: false, dock: 'left', pinned: true, x: 0, y: 0, w: 0, h: 0 },
    ]);
    // hidden + pinned restored; docked → not placed (PanelHost re-docks).
    expect(panel('kernel')).toMatchObject({ open: false, dock: 'left', pinned: true, placed: false });
    // ensureRails must NOT clobber the restored kernel; it only adds lifeline.
    s.ensureRails();
    expect(usePanelStore.getState().panels.filter((p) => p.id === 'kernel')).toHaveLength(1);
    expect(panel('kernel')).toMatchObject({ open: false, pinned: true });
    expect(panel('lifeline')).toMatchObject({ open: true, dock: 'right' });
  });

  it('hydrateRails restores a floating (moved) rail with its geometry + placed', () => {
    const s = usePanelStore.getState();
    s.hydrateRails([
      { kind: 'lifeline', open: true, dock: null, pinned: false, x: 300, y: 120, w: 320, h: 480 },
    ]);
    expect(panel('lifeline')).toMatchObject({
      dock: null,
      placed: true,
      x: 300,
      y: 120,
      w: 320,
      h: 480,
    });
  });
});
