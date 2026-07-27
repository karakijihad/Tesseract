import { describe, it, expect, beforeEach } from 'vitest';
import { useSurfacesStore } from './surfaces';
import { usePanelStore } from '../cockpit/panelStore';
import { __resetZForTest } from '../cockpit/zStack';

describe('shared z-order', () => {
  beforeEach(() => {
    __resetZForTest();
    useSurfacesStore.setState({ byView: {}, highlights: {}, liveZ: {} });
    usePanelStore.setState({ panels: [], topZ: 10 });
  });

  it('raiseSurface assigns an increasing shared z', () => {
    useSurfacesStore.getState().raiseSurface('a');
    useSurfacesStore.getState().raiseSurface('b');
    expect(useSurfacesStore.getState().liveZ.b).toBeGreaterThan(useSurfacesStore.getState().liveZ.a);
  });

  it('last-focused wins across panel then surface', () => {
    usePanelStore.getState().openPanel('pulse');
    const panelZ = usePanelStore.getState().panels.find((p) => p.id === 'pulse')!.z;
    useSurfacesStore.getState().raiseSurface('s1');
    expect(useSurfacesStore.getState().liveZ.s1).toBeGreaterThan(panelZ);
  });

  it('focusing a panel raises it above a surface that was on top', () => {
    usePanelStore.getState().openPanel('pulse');
    useSurfacesStore.getState().raiseSurface('s1'); // surface now on top
    usePanelStore.getState().focus('pulse');        // operator clicks the panel
    const panelZ = usePanelStore.getState().panels.find((p) => p.id === 'pulse')!.z;
    expect(panelZ).toBeGreaterThan(useSurfacesStore.getState().liveZ.s1);
  });
});
