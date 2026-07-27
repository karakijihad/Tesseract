// SC-2/SC-3 + panel-polish — renders the open panels into the `.panel-host`
// slot. Geometry is centered/edge-docked here (only the host can measure the
// slot). Panel-polish adds: the maximize rect (stage minus the open docked
// rails) and a ResizeObserver that keeps the layout responsive — docked rails
// re-hug the edges and floating panels stay on-screen when the viewport changes.

import { useLayoutEffect, useMemo, useRef, useState } from 'react';

import { usePanelStore, isRailKind } from './panelStore';
import { GlassPanel } from './GlassPanel';

const MAX_W = 820;
const MAX_H = 580;
const MARGIN_X = 64;
const MARGIN_Y = 48;
const CASCADE = 28;

const RAIL_W = 282;
const DOCK_MARGIN = 14;
const MAX_GAP = 14; // gap between a docked rail and a maximized panel

interface Slot {
  w: number;
  h: number;
}

export function PanelHost() {
  const panels = usePanelStore((s) => s.panels);
  const place = usePanelStore((s) => s.place);
  const ref = useRef<HTMLDivElement>(null);
  const [slot, setSlot] = useState<Slot | null>(null);

  // Initial placement: center view panels (cascaded), edge-dock rails.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const unplaced = panels.filter((p) => !p.placed);
    if (unplaced.length === 0) return;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    let cascadeIdx = panels.filter((p) => p.placed && !isRailKind(p.kind)).length;
    unplaced.forEach((p) => {
      if (p.dock) {
        const w = RAIL_W;
        const h = Math.max(0, Math.round(rect.height) - DOCK_MARGIN * 2);
        const x = p.dock === 'left' ? DOCK_MARGIN : Math.round(rect.width) - RAIL_W - DOCK_MARGIN;
        place(p.id, { x: Math.max(0, x), y: DOCK_MARGIN, w, h });
        return;
      }
      // Center new view panels in the rail-clear area so opening a tab never
      // covers a docked rail.
      const lr = panels.find((q) => q.id === 'kernel' && q.open && q.dock === 'left' && q.placed);
      const rr = panels.find((q) => q.id === 'lifeline' && q.open && q.dock === 'right' && q.placed);
      const clearX = lr ? lr.x + lr.w + MAX_GAP : DOCK_MARGIN;
      const clearRight = rr ? rr.x - MAX_GAP : Math.round(rect.width) - DOCK_MARGIN;
      const clearW = Math.max(1, clearRight - clearX);
      const w = Math.min(MAX_W, Math.max(1, clearW - MARGIN_X));
      const h = Math.min(MAX_H, Math.round(rect.height) - MARGIN_Y);
      const offset = cascadeIdx * CASCADE;
      cascadeIdx += 1;
      const x = clearX + Math.max(0, Math.round((clearW - w) / 2) + offset);
      const y = Math.max(0, Math.round((rect.height - h) / 2) + offset);
      place(p.id, { x, y, w, h });
    });
  }, [panels, place]);

  // Responsive: track the slot size; on resize, re-dock rails to the new edges
  // and clamp floating panels back on-screen. Reads the store at call time so it
  // doesn't need `panels` in deps (avoids re-subscribing the observer per
  // geometry change); place() changes the store but not the element size, so no
  // observer loop.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      const rect = el.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) return;
      const W = Math.round(rect.width);
      const H = Math.round(rect.height);
      setSlot({ w: W, h: H });
      for (const p of usePanelStore.getState().panels) {
        if (!p.placed) continue;
        if (p.dock) {
          const w = RAIL_W;
          const h = Math.max(0, H - DOCK_MARGIN * 2);
          const x = p.dock === 'left' ? DOCK_MARGIN : W - RAIL_W - DOCK_MARGIN;
          const cx = Math.max(0, x);
          if (p.x !== cx || p.y !== DOCK_MARGIN || p.w !== w || p.h !== h) {
            place(p.id, { x: cx, y: DOCK_MARGIN, w, h });
          }
        } else if (!p.maximized && !p.pinned) {
          // Clamp only UNPINNED floating panels back on-screen. A pinned panel's
          // geometry is the operator's saved default — clamping it here would
          // get persisted (layoutPersistence) and permanently shrink the saved
          // size on a transient viewport change. Pinned panels keep their size;
          // the operator can drag if a small screen pushes one off-edge.
          const w = Math.min(p.w, W - DOCK_MARGIN * 2);
          const h = Math.min(p.h, H - DOCK_MARGIN * 2);
          const x = Math.min(Math.max(0, p.x), Math.max(0, W - w - DOCK_MARGIN));
          const y = Math.min(Math.max(0, p.y), Math.max(0, H - h - DOCK_MARGIN));
          if (x !== p.x || y !== p.y || w !== p.w || h !== p.h) place(p.id, { x, y, w, h });
        }
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [place]);

  // The rect a maximized panel fills: a CENTERED container that expands in width
  // + height. The horizontal inset is symmetric (the wider of the two rail
  // intrusions applied to both sides), so the panel is centered in the stage —
  // not anchored to one rail — and the rails stay visible at the edges.
  const maximizeRect = useMemo(() => {
    if (!slot) return null;
    const leftRail = panels.find((p) => p.id === 'kernel' && p.open && p.dock === 'left' && p.placed);
    const rightRail = panels.find((p) => p.id === 'lifeline' && p.open && p.dock === 'right' && p.placed);
    const leftIntrude = leftRail ? leftRail.x + leftRail.w + MAX_GAP : DOCK_MARGIN;
    const rightIntrude = rightRail ? slot.w - rightRail.x + MAX_GAP : DOCK_MARGIN;
    const inset = Math.max(leftIntrude, rightIntrude);
    // Fill the centered container exactly (no min-width floor — a floor wider
    // than the available space would push the panel under a rail). x === inset,
    // so the rails always stay clear.
    const w = Math.max(1, slot.w - inset * 2);
    return { x: Math.round((slot.w - w) / 2), y: DOCK_MARGIN, w, h: slot.h - DOCK_MARGIN * 2 };
  }, [slot, panels]);

  return (
    <div className="panel-host" ref={ref}>
      {panels.map((p) => (
        <GlassPanel key={p.id} panel={p} maximizeRect={maximizeRect} bounds={slot} />
      ))}
    </div>
  );
}
