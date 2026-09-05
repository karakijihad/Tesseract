// The rect a maximized thing fills, and the one place it is worked out.
//
// A view panel and a canvas surface both maximize onto the same stage, so
// "full screen" has to mean the same box for both: a CENTERED container that
// expands in width and height, inset by the wider of the two rail intrusions
// on BOTH sides, so it is centered in the stage rather than anchored to one
// rail and the rails stay visible at the edges.

import { useMemo } from "react";

import { usePanelStore } from "./panelStore";

export const DOCK_MARGIN = 14;
export const MAX_GAP = 14; // gap between a docked rail and a maximized panel

export interface StageSlot {
  w: number;
  h: number;
}

export interface MaximizeRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

interface RailGeometry {
  id: string;
  open: boolean;
  dock: string | null;
  placed: boolean;
  x: number;
  w: number;
}

export function computeMaximizeRect(
  slot: StageSlot | null,
  panels: RailGeometry[],
): MaximizeRect | null {
  if (!slot) return null;
  const leftRail = panels.find(
    (p) => p.id === "kernel" && p.open && p.dock === "left" && p.placed,
  );
  const rightRail = panels.find(
    (p) => p.id === "lifeline" && p.open && p.dock === "right" && p.placed,
  );
  const leftIntrude = leftRail ? leftRail.x + leftRail.w + MAX_GAP : DOCK_MARGIN;
  const rightIntrude = rightRail ? slot.w - rightRail.x + MAX_GAP : DOCK_MARGIN;
  const inset = Math.max(leftIntrude, rightIntrude);
  // Fill the centered container exactly (no min-width floor — a floor wider
  // than the available space would push the panel under a rail). x === inset,
  // so the rails always stay clear.
  const w = Math.max(1, slot.w - inset * 2);
  return {
    x: Math.round((slot.w - w) / 2),
    y: DOCK_MARGIN,
    w,
    h: slot.h - DOCK_MARGIN * 2,
  };
}

/** The same rect, recomputed when the stage resizes or a rail moves. */
export function useMaximizeRect(slot: StageSlot | null): MaximizeRect | null {
  const panels = usePanelStore((s) => s.panels);
  return useMemo(() => computeMaximizeRect(slot, panels), [slot, panels]);
}
