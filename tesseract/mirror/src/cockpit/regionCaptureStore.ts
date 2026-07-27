// Slice 2 — region capture → TARS vision. The operator drags a rectangle over
// the cockpit; the region is captured to a PNG and sent (with a typed
// instruction) through the EXISTING chat image-attachment path so TARS reasons
// over it. Store = the capture state machine; the rest is a pure rect helper +
// the DOM→image capture util.

import { create } from 'zustand';
import * as htmlToImage from 'html-to-image';

export interface CaptureRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

// idle → selecting (drag a marquee) → capturing (rendering) → composing (type +
// send) → idle.
export type CaptureMode = 'idle' | 'selecting' | 'capturing' | 'composing';

interface RegionCaptureStore {
  mode: CaptureMode;
  rect: CaptureRect | null;
  preview: string | null; // dataURL thumbnail
  file: File | null; // the captured PNG, uploaded on send
  enter: () => void;
  setRect: (rect: CaptureRect | null) => void;
  beginCapture: () => void;
  setCaptured: (file: File, preview: string, rect: CaptureRect) => void;
  cancel: () => void;
}

export const useRegionCaptureStore = create<RegionCaptureStore>((set) => ({
  mode: 'idle',
  rect: null,
  preview: null,
  file: null,
  enter: () => set({ mode: 'selecting', rect: null, preview: null, file: null }),
  setRect: (rect) => set({ rect }),
  beginCapture: () => set({ mode: 'capturing' }),
  setCaptured: (file, preview, rect) => set({ mode: 'composing', file, preview, rect }),
  cancel: () => set({ mode: 'idle', rect: null, preview: null, file: null }),
}));

const MIN_CAPTURE = 8; // ignore accidental click-without-drag
const CAPTURE_TIMEOUT_MS = 12_000; // toCanvas fetches fonts/images — bound the wait

/** Normalize a drag (two points) into a positive-dimension rect. Pure. */
export function rectFromPoints(x0: number, y0: number, x1: number, y1: number): CaptureRect {
  return { x: Math.min(x0, x1), y: Math.min(y0, y1), w: Math.abs(x1 - x0), h: Math.abs(y1 - y0) };
}

/** A drag worth capturing (not a stray click). Pure. */
export function isCapturable(rect: CaptureRect): boolean {
  return rect.w >= MIN_CAPTURE && rect.h >= MIN_CAPTURE;
}

/**
 * Capture the cockpit region under `rect` (viewport coords) to a PNG File +
 * preview dataURL. Renders the whole cockpit DOM, then crops. The capture
 * overlay (`.region-capture-root`) is filtered out so the marquee isn't in the
 * shot. NOTE: the WebGL orb (GlobalCanvas) may render blank — html-to-image
 * can't reliably read a WebGL canvas back; panel/chrome DOM captures fine, which
 * is the point of "select a panel region and ask TARS".
 */
export async function captureRegion(rect: CaptureRect): Promise<{ file: File; preview: string }> {
  const render = htmlToImage.toCanvas(document.body, {
    pixelRatio: 1,
    cacheBust: true,
    filter: (node) =>
      !(node instanceof HTMLElement && node.classList.contains('region-capture-root')),
  });
  // Bound the wait — a stalled font/image fetch must not hang the state machine
  // in `capturing` forever (the caller's catch resets to idle on rejection).
  const timeout = new Promise<never>((_, reject) =>
    setTimeout(() => reject(new Error('region capture timed out')), CAPTURE_TIMEOUT_MS),
  );
  const full = await Promise.race([render, timeout]);
  const crop = document.createElement('canvas');
  crop.width = Math.max(1, Math.round(rect.w));
  crop.height = Math.max(1, Math.round(rect.h));
  const ctx = crop.getContext('2d');
  if (!ctx) throw new Error('2d context unavailable');
  ctx.drawImage(full, rect.x, rect.y, rect.w, rect.h, 0, 0, crop.width, crop.height);
  const preview = crop.toDataURL('image/png');
  const blob = await new Promise<Blob | null>((resolve) => crop.toBlob(resolve, 'image/png'));
  if (!blob) throw new Error('capture encode failed');
  const file = new File([blob], 'region-capture.png', { type: 'image/png' });
  return { file, preview };
}
