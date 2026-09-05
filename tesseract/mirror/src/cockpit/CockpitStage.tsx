import { useEffect, useRef, useState } from "react";

import { TopStatusHud } from "../components/cockpit/TopStatusHud";
import { BottomHud } from "../components/cockpit/BottomHud";
import { OrbAnchor } from "../canvas/OrbAnchor";
import { SurfaceLayer } from "../canvas/SurfaceLayer";
import { SurfaceDock } from "./SurfaceDock";
import { restoreLanes } from "../canvas/laneRestore";
import { McpApprovalsPane } from "./McpApprovalsPane";
import { OrbCaptions } from "./OrbCaptions";
import { ParkedAsksPane } from "./ParkedAsksPane";
import { PanelHost } from "./PanelHost";
import { RailDockTabs } from "../components/cockpit/RailDockTabs";
import { usePanelStore } from "./panelStore";
import { useMaximizeRect, type StageSlot } from "./maximizeRect";
import { installLayoutPersistence } from "./layoutPersistence";

// SC-1/2/3 — the fixed spatial cockpit. The assistant orb (GlobalCanvas, full-screen
// behind at z-index 0, clamped above `.cockpit-hud`) is the permanent
// centerpiece; a top status pill + bottom command HUD float over it, and every
// surface — summoned views AND the Kernel/Lifeline rails — is a movable glass
// panel in the same `PanelHost` over a full-width stage. SC-3 retired the grid
// asides: the rails are now dockable panels (default-docked at the edges,
// drag-to-float, K/L to hide/show, reset re-docks). `ensureRails` seeds the two
// rail panels once on mount. The cockpit is permanently in the immersive
// `data-view="orb"` mode — atmosphere + glass HUD — so the orb keeps the exact
// rendering it had on the old OrbView.

export function CockpitStage() {
  const ensureRails = usePanelStore((s) => s.ensureRails);
  const stageRef = useRef<HTMLDivElement>(null);
  const [slot, setSlot] = useState<StageSlot | null>(null);
  // The surface layer does not read the panel store (the canvas keeps its own
  // import graph), so the stage measures itself and hands the maximize rect
  // down. `PanelHost` works the same rect out from its own slot, which is the
  // same box: both are `inset: 0` inside `.cockpit-stage`.
  const maximizeRect = useMaximizeRect(slot);

  useEffect(() => {
    const el = stageRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const cr = entries[0]?.contentRect;
      if (cr) setSlot({ w: cr.width, h: cr.height });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    // Hydrate the operator's pinned panels + rails (default layout) and keep
    // storage in sync, then seed any rails still missing. Both are idempotent.
    installLayoutPersistence();
    ensureRails();
    // Re-surface last session's surviving lanes as cards (idempotent against
    // any already restored from persisted surfaces). Fire-and-forget; a
    // backend hiccup on boot must not surface as an unhandled rejection.
    void restoreLanes("orb").catch(() => undefined);
  }, [ensureRails]);

  return (
    <div className="cockpit-shell" data-view="orb">
      <TopStatusHud />
      <main className="cockpit-center">
        <div className="cockpit-stage" ref={stageRef}>
          <OrbAnchor />
          {/* Ambient captions of the assistant's latest line, faded under the orb —
              voice-first "what the assistant just said" without opening the Chat tab.
              Pointer-transparent; sits below the panels. Dismissable (HUD CC). */}
          <OrbCaptions />
          {/* SC-2/3 — summoned view panels + the docked rails, all glass over
              the orb. When the rails are hidden and no view is open, the stage
              is the bare orb home. */}
          <PanelHost />
          {/* ASK-over-MCP approvals — floating alert, self-hidden when idle. */}
          <McpApprovalsPane />
          {/* trio W4 — parked background-spawn asks (ask-instead-of-die),
              self-hidden when nothing is parked. */}
          <ParkedAsksPane />
          {/* SC-4 — agent-spawned Surface-Protocol cards (CV-1 lanes,
              delegate transcripts, command-spawned web/file/folder content)
              re-homed over the orb. SC-1 dropped the tldraw center that used to
              host them; this is their home now. Pointer-transparent overlay —
              only the cards capture input. */}
          <SurfaceLayer view="orb" maximizeRect={maximizeRect} />
        </div>
      </main>
      {/* Everything put away — minimized cards AND minimized view panels — as a
          row of chips just above the HUD.

          A sibling of the stage, not a child of it: `.cockpit-stage` sets
          `perspective` and so is a stacking context, which traps every panel's
          z inside it and would leave the dock under any focused panel. Out
          here it is always reachable, which for a surface card matters because
          the dock is its only way back. It is also outside `SurfaceLayer` so
          the canvas keeps its own import graph: the dock reads the panel
          store, which reaches every view. */}
      <SurfaceDock view="orb" />
      <div className="cockpit-hud">
        <BottomHud />
      </div>
      {/* A hidden rail reappears from its own edge. */}
      <RailDockTabs />
    </div>
  );
}
