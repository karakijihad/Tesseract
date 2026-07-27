// AS-2 — make the activity pill (chip) + activity map (UI) movable along the
// cockpit canvas. Mirrors the window-listener drag pattern in GlassPanel /
// SurfaceLayer (no setPointerCapture). Operator placement persists per-element
// via localStorage so a deliberate position survives the map re-opening. A
// drag that never crosses the threshold reads as a click, so the pill still
// toggles the map.

import { useCallback, useRef, useState } from 'react';
import type { PointerEvent as ReactPointerEvent } from 'react';

export interface DragPos {
  x: number;
  y: number;
}

const DRAG_THRESHOLD = 4;
const EDGE_MARGIN = 40; // keep at least this much of the element on-screen

function load(key: string): DragPos | null {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const v = JSON.parse(raw) as Partial<DragPos>;
    if (typeof v.x === 'number' && typeof v.y === 'number') return { x: v.x, y: v.y };
  } catch {
    // corrupt entry — fall back to the CSS default
  }
  return null;
}

export function useDraggable(storageKey: string) {
  const ref = useRef<HTMLElement | null>(null);
  const [pos, setPos] = useState<DragPos | null>(() => load(storageKey));
  const movedRef = useRef(false);

  const onDragStart = useCallback(
    (e: ReactPointerEvent) => {
      const el = ref.current;
      if (!el) return;
      // preventDefault stops text-selection on a div handle, but on a <button>
      // handle (the pill) it can suppress the follow-up `click` on some mobile
      // WebViews — which would break the open/close toggle. didDrag() already
      // distinguishes drag from click, so skip it for button handles.
      if ((e.currentTarget as HTMLElement).tagName !== 'BUTTON') e.preventDefault();
      const parent = (el.offsetParent as HTMLElement | null) ?? el.parentElement;
      const pr = parent?.getBoundingClientRect() ?? { left: 0, top: 0, width: Infinity, height: Infinity };
      const er = el.getBoundingClientRect();
      // Visual top-left relative to the offset parent — reproduces the rendered
      // position (incl. the CSS centering transform) so the first drag doesn't
      // jump when we switch to explicit left/top.
      const bx = pos?.x ?? er.left - pr.left;
      const by = pos?.y ?? er.top - pr.top;
      const ox = e.clientX;
      const oy = e.clientY;
      const maxX = Math.max(0, pr.width - EDGE_MARGIN);
      const maxY = Math.max(0, pr.height - EDGE_MARGIN);
      movedRef.current = false;
      let last: DragPos = { x: bx, y: by };
      const onMove = (ev: PointerEvent) => {
        const dx = ev.clientX - ox;
        const dy = ev.clientY - oy;
        if (Math.abs(dx) > DRAG_THRESHOLD || Math.abs(dy) > DRAG_THRESHOLD) movedRef.current = true;
        last = {
          x: Math.min(Math.max(0, bx + dx), maxX),
          y: Math.min(Math.max(0, by + dy), maxY),
        };
        setPos(last);
      };
      const onUp = () => {
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', onUp);
        if (movedRef.current) {
          try {
            localStorage.setItem(storageKey, JSON.stringify(last));
          } catch {
            // storage quota / disabled — position is still live for this session
          }
        }
      };
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onUp);
    },
    [pos, storageKey],
  );

  // Once moved, pin to explicit coords and cancel the CSS bottom/center anchor.
  const style = pos
    ? ({ left: pos.x, top: pos.y, right: 'auto', bottom: 'auto', transform: 'none' } as const)
    : undefined;

  // True for the click immediately following a real drag, so callers can
  // suppress the click action (e.g. the pill's open/close toggle).
  const didDrag = useCallback(() => movedRef.current, []);

  return { ref, style, onDragStart, didDrag };
}
