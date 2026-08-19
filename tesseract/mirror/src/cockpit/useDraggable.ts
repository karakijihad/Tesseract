// AS-2 — make a floating cockpit card movable, and (opt-in) resizable from any
// edge or corner. Mirrors the window-listener drag pattern in GlassPanel /
// SurfaceLayer (no setPointerCapture). Operator placement persists per-element
// via localStorage so a deliberate position survives the card re-opening. A
// drag that never crosses the threshold reads as a click, so a button handle
// still toggles what it opens.

import { useCallback, useRef, useState } from 'react';
import type { PointerEvent as ReactPointerEvent } from 'react';

import type { ResizeDir } from '../components/common/ResizeHandles';

export interface DragPos {
  x: number;
  y: number;
}

export interface DragSize {
  w: number;
  h: number;
}

interface Stored extends Partial<DragPos>, Partial<DragSize> {}

interface DraggableOptions {
  /** Enables the resize half. Floors below which the card cannot be dragged
   *  shut — a card resized to nothing has no handle left to grab. */
  minW?: number;
  minH?: number;
}

const DRAG_THRESHOLD = 4;
const EDGE_MARGIN = 40; // keep at least this much of the element on-screen

function load(key: string): Stored {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return {};
    const v = JSON.parse(raw) as Stored;
    const out: Stored = {};
    if (typeof v.x === 'number' && typeof v.y === 'number') {
      out.x = v.x;
      out.y = v.y;
    }
    if (typeof v.w === 'number' && typeof v.h === 'number') {
      out.w = v.w;
      out.h = v.h;
    }
    return out;
  } catch {
    // corrupt entry — fall back to the CSS default
    return {};
  }
}

function save(key: string, pos: DragPos | null, size: DragSize | null): void {
  try {
    if (!pos && !size) {
      localStorage.removeItem(key);
      return;
    }
    localStorage.setItem(key, JSON.stringify({ ...(pos ?? {}), ...(size ?? {}) }));
  } catch {
    // storage quota / disabled — the geometry is still live for this session
  }
}

export function useDraggable(storageKey: string, options: DraggableOptions = {}) {
  const { minW = 220, minH = 160 } = options;
  const ref = useRef<HTMLElement | null>(null);
  const initial = useRef(load(storageKey)).current;
  const [pos, setPos] = useState<DragPos | null>(
    initial.x !== undefined && initial.y !== undefined
      ? { x: initial.x, y: initial.y }
      : null,
  );
  const [size, setSize] = useState<DragSize | null>(
    initial.w !== undefined && initial.h !== undefined
      ? { w: initial.w, h: initial.h }
      : null,
  );
  const movedRef = useRef(false);

  // Visual top-left relative to the offset parent — reproduces the rendered
  // position (incl. any CSS centering transform) so the first interaction
  // doesn't jump when we switch to explicit left/top.
  const measure = useCallback(() => {
    const el = ref.current;
    if (!el) return null;
    const parent = (el.offsetParent as HTMLElement | null) ?? el.parentElement;
    const pr = parent?.getBoundingClientRect() ?? {
      left: 0,
      top: 0,
      width: Infinity,
      height: Infinity,
    };
    const er = el.getBoundingClientRect();
    return {
      x: pos?.x ?? er.left - pr.left,
      y: pos?.y ?? er.top - pr.top,
      w: size?.w ?? er.width,
      h: size?.h ?? er.height,
      pw: pr.width,
      ph: pr.height,
    };
  }, [pos, size]);

  const onDragStart = useCallback(
    (e: ReactPointerEvent) => {
      const base = measure();
      if (!base) return;
      // preventDefault stops text-selection on a div handle, but on a <button>
      // handle it can suppress the follow-up `click` on some mobile WebViews —
      // which would break an open/close toggle. didDrag() already distinguishes
      // drag from click, so skip it for button handles.
      if ((e.currentTarget as HTMLElement).tagName !== 'BUTTON') e.preventDefault();
      const ox = e.clientX;
      const oy = e.clientY;
      const maxX = Math.max(0, base.pw - EDGE_MARGIN);
      const maxY = Math.max(0, base.ph - EDGE_MARGIN);
      movedRef.current = false;
      let last: DragPos = { x: base.x, y: base.y };
      const onMove = (ev: PointerEvent) => {
        const dx = ev.clientX - ox;
        const dy = ev.clientY - oy;
        if (Math.abs(dx) > DRAG_THRESHOLD || Math.abs(dy) > DRAG_THRESHOLD)
          movedRef.current = true;
        last = {
          x: Math.min(Math.max(0, base.x + dx), maxX),
          y: Math.min(Math.max(0, base.y + dy), maxY),
        };
        setPos(last);
      };
      const onUp = () => {
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', onUp);
        if (movedRef.current) save(storageKey, last, size);
      };
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onUp);
    },
    [measure, size, storageKey],
  );

  const onResizeStart = useCallback(
    (dir: ResizeDir) => (e: ReactPointerEvent) => {
      const base = measure();
      if (!base) return;
      e.preventDefault();
      e.stopPropagation();
      const west = dir.includes('w');
      const east = dir.includes('e');
      const north = dir.includes('n');
      const south = dir.includes('s');
      const ox = e.clientX;
      const oy = e.clientY;
      let lastPos: DragPos = { x: base.x, y: base.y };
      let lastSize: DragSize = { w: base.w, h: base.h };
      const onMove = (ev: PointerEvent) => {
        const dx = ev.clientX - ox;
        const dy = ev.clientY - oy;
        let { x, y } = { x: base.x, y: base.y };
        let w = base.w;
        let h = base.h;
        if (east) w = Math.max(minW, base.w + dx);
        if (south) h = Math.max(minH, base.h + dy);
        // Dragging a leading edge moves the card as it resizes, so the edge the
        // pointer is NOT on stays where the operator left it.
        if (west) {
          w = Math.max(minW, base.w - dx);
          x = base.x + (base.w - w);
        }
        if (north) {
          h = Math.max(minH, base.h - dy);
          y = base.y + (base.h - h);
        }
        x = Math.max(0, x);
        y = Math.max(0, y);
        w = Math.min(w, Math.max(minW, base.pw - x));
        h = Math.min(h, Math.max(minH, base.ph - y));
        lastPos = { x, y };
        lastSize = { w, h };
        setPos(lastPos);
        setSize(lastSize);
      };
      const onUp = () => {
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', onUp);
        save(storageKey, lastPos, lastSize);
      };
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onUp);
    },
    [measure, minH, minW, storageKey],
  );

  /** Back to where the stylesheet puts it, at the size the stylesheet gives
   *  it — and forget the stored geometry, so it stays there next launch. */
  const reset = useCallback(() => {
    setPos(null);
    setSize(null);
    save(storageKey, null, null);
  }, [storageKey]);

  const moved = pos !== null || size !== null;

  // Once moved, pin to explicit coords and cancel the CSS bottom/center anchor.
  // A resized card also drops `max-height`, which would otherwise cap the
  // height the operator just chose.
  const style =
    pos || size
      ? ({
          ...(pos
            ? { left: pos.x, top: pos.y, right: 'auto', bottom: 'auto', transform: 'none' }
            : {}),
          ...(size ? { width: size.w, height: size.h, maxHeight: 'none' } : {}),
        } as const)
      : undefined;

  // True for the click immediately following a real drag, so callers can
  // suppress the click action (e.g. a toggle button handle).
  const didDrag = useCallback(() => movedRef.current, []);

  return { ref, style, onDragStart, onResizeStart, reset, moved, didDrag };
}
