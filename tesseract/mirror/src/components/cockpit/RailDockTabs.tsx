import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

import { usePanelStore } from "../../cockpit/panelStore";
import { Hint } from "../ui/Hint";
import { EdgeTab } from '../common/EdgeTab';

const RAILS = [
  { id: "kernel", side: "left", label: "Kernel", glyph: "▸" },
  { id: "lifeline", side: "right", label: "Monitor", glyph: "◂" },
] as const;

/** A hidden rail's way back — the bottom HUD's edge tab, turned sideways.
 *
 * The two letter buttons in the HUD's stage stack did this before. They were a
 * control for a panel, parked somewhere the panel is not, in a stack that also
 * held the orb and caption toggles. A panel that hides itself with its own ×
 * should reappear from its own edge, which is where an operator reaches.
 */
export function RailDockTabs() {
  const panels = usePanelStore((s) => s.panels);
  const toggleRail = usePanelStore((s) => s.toggleRail);
  // Portals have no server renderer, and CockpitStage is asserted with
  // `renderToStaticMarkup`. Mounting first keeps this client-only.
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  const hidden = RAILS.filter(
    (r) => !(panels.find((p) => p.id === r.id)?.open ?? false),
  );
  if (!mounted || hidden.length === 0) return null;

  return createPortal(
    <>
      {hidden.map((r) => (
        <Hint key={r.id} label={`Show ${r.label}`} position="right" maxWidth={140}>
          <EdgeTab
            side={r.side}
            onClick={() => toggleRail(r.id)}
            ariaLabel={`Show the ${r.label} rail`}
          >
            {r.glyph}
          </EdgeTab>
        </Hint>
      ))}
    </>,
    document.body,
  );
}
