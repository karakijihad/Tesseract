import type { PointerEvent as ReactPointerEvent } from "react";

export type ResizeDir = "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";

const DIRS: ResizeDir[] = ["n", "s", "e", "w", "ne", "nw", "se", "sw"];

/** The unit vector each direction grows: −1 anchors the leading edge (left or
 *  top), +1 the trailing edge, 0 is fixed. Exported because callers that took
 *  vectors before the handles were shared still think in them. */
export const RESIZE_VECTOR: Record<ResizeDir, { dx: -1 | 0 | 1; dy: -1 | 0 | 1 }> = {
  n: { dx: 0, dy: -1 },
  s: { dx: 0, dy: 1 },
  e: { dx: 1, dy: 0 },
  w: { dx: -1, dy: 0 },
  ne: { dx: 1, dy: -1 },
  nw: { dx: -1, dy: -1 },
  se: { dx: 1, dy: 1 },
  sw: { dx: -1, dy: 1 },
};

interface ResizeHandlesProps {
  /** Curried per direction — the caller owns the geometry, this owns where the
   *  grab targets are and how wide they have to be to be grabbable. */
  onResizeStart: (dir: ResizeDir) => (e: ReactPointerEvent) => void;
  /** For a card that clips its overflow, where an overhanging target would be
   *  invisible: the handles sit just inside the edge instead. */
  inset?: boolean;
}

/** The eight edges and corners of a resizable card — every one in the app.
 *
 * Glass panels, canvas surfaces and the Routing map each had their own set,
 * with different target widths, so a resize that felt fine on one surface
 * missed on the next. The part that is easy to get wrong is not the maths: it
 * is that a 1px edge cannot be caught with a pointer. The targets are 10px,
 * the corners outrank the edges by coming last, and the south-east corner
 * carries the diagonal grip that says the card resizes at all.
 *
 * The container must be positioned (`relative` or `absolute`).
 */
export function ResizeHandles({ onResizeStart, inset = false }: ResizeHandlesProps) {
  return (
    <>
      {DIRS.map((dir) => (
        <div
          key={dir}
          className={`resize-handle resize-handle--${dir}${inset ? " is-inset" : ""}`}
          aria-hidden="true"
          onPointerDown={onResizeStart(dir)}
        />
      ))}
    </>
  );
}
