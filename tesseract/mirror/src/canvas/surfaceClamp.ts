// Keeping a surface card inside the layer that draws it.
//
// `.surface-layer` is `overflow: hidden`, so a card dragged past an edge is
// clipped away with the title bar it is grabbed by — there is no gesture that
// brings it back. Both sibling systems already solve this (`GlassPanel`
// hard-clamps inside its container, `useDraggable` keeps an edge margin) and
// the layer already measures the bound it needs, because maximize uses it.
//
// An unmeasured bound (0) imposes no cap: clamping to zero would pin every
// card to the top-left corner for the frame before the layer measures itself.

export interface LayerBounds {
  w: number;
  h: number;
}

export function clampX(x: number, w: number, bounds: LayerBounds): number {
  if (!bounds.w) return x;
  return Math.min(Math.max(0, x), Math.max(0, bounds.w - w));
}

export function clampY(y: number, h: number, bounds: LayerBounds): number {
  if (!bounds.h) return y;
  return Math.min(Math.max(0, y), Math.max(0, bounds.h - h));
}

/** How wide a card anchored at `originX` may grow before its trailing edge
 *  leaves the layer. `minW` keeps a degenerate bound from collapsing it. */
export function maxW(
  originX: number,
  bounds: LayerBounds,
  minW: number,
): number {
  return bounds.w
    ? Math.max(minW, bounds.w - originX)
    : Number.POSITIVE_INFINITY;
}

export function maxH(
  originY: number,
  bounds: LayerBounds,
  minH: number,
): number {
  return bounds.h
    ? Math.max(minH, bounds.h - originY)
    : Number.POSITIVE_INFINITY;
}
