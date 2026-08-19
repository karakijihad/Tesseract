// A closed panel is HIDDEN, not unmounted — `GlassPanel` adds
// `glass-panel--hidden` and leaves the view in the tree, deliberately, so a
// lane stream or a terminal keeps running behind it. The consequence caught
// the Conscience tab: a view that fetches in a mount effect fetches once per
// app launch, so closing and reopening it shows whatever it read at boot. The
// trigger a view actually wants is the visibility TRANSITION.

import { useEffect, useRef } from 'react';

import { usePanelStore, type PanelKind } from '../cockpit/panelStore';

export function usePanelVisible(kind: PanelKind): boolean {
  return usePanelStore((s) => {
    const panel = s.panels.find((p) => p.id === kind);
    return !!panel && panel.open && !panel.minimized;
  });
}

/** Run `refresh` when this panel becomes visible, including the first time.
 *
 * Replaces a bare mount effect rather than joining one: the first render of a
 * panel that is being opened is already a hidden→visible transition, so a view
 * using this needs no separate fetch-on-mount. */
export function useRefreshOnVisible(kind: PanelKind, refresh: () => void): void {
  const visible = usePanelVisible(kind);
  const wasVisible = useRef(false);
  useEffect(() => {
    if (visible && !wasVisible.current) refresh();
    wasVisible.current = visible;
  }, [visible, refresh]);
}
