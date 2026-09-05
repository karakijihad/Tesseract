// Y-2 — the Surface Protocol overlay. Renders agent-spawned surface cards
// above the tldraw canvas, independent of tldraw's shape tree (the two
// layers are deliberately separate — _shared/surface-protocol.md). The
// layer itself is pointer-transparent; only the cards capture input, so the
// operator can still pan/draw the tldraw canvas in the gaps between cards.
//
// Cards are positioned in container coordinates (camera-synced panning is a
// deferred additive enhancement). Drag/resize/close mutate the store
// optimistically and POST the operator event back via `emitSurfaceEvent`.

import { CloseButton } from "../components/common/CloseButton";
import { IconButton } from "../components/common/IconButton";
import {
  ResizeHandles,
  RESIZE_VECTOR,
} from "../components/common/ResizeHandles";
import { useCallback, useEffect, useRef, useState } from "react";

import { useSurfacesStore } from "../stores/surfaces";
import { useWebSocketStore } from "../stores/websocket";
import { reportSurfaceRender } from "./protocol/events";
import type {
  OperatorEvent,
  ReportRender,
  SurfaceDescriptor,
} from "./protocol/types";
import { getRenderer, RENDERERS } from "./renderers";
import {
  clampX,
  clampY,
  maxH,
  maxW,
  type LayerBounds,
} from "./surfaceClamp";
import { ErrorBoundary } from "../components/common/ErrorBoundary";
import { Hint } from '../components/ui/Hint';
import {
  MaximizeIcon,
  MinimizeIcon,
  PinIcon,
} from '../components/common/icons';
import type { MaximizeRect } from '../cockpit/maximizeRect';

interface SurfaceLayerProps {
  view: string;
  // The rect a maximized card fills, worked out once for the whole stage so a
  // full-screen card and a full-screen view panel are the same box. Null while
  // the stage has not been measured, which leaves a card at its own geometry.
  maximizeRect?: MaximizeRect | null;
}


export function SurfaceLayer({ view, maximizeRect = null }: SurfaceLayerProps) {
  const hydrate = useSurfacesStore((s) => s.hydrate);
  const surfaces = useSurfacesStore((s) => s.byView[view]);
  const layerRef = useRef<HTMLDivElement>(null);
  // Measured overlay bounds — a maximized card fills these (container coords).
  const [bounds, setBounds] = useState<LayerBounds>({ w: 0, h: 0 });

  useEffect(() => {
    void hydrate(view);
  }, [hydrate, view]);

  useEffect(() => {
    const el = layerRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const cr = entries[0]?.contentRect;
      if (cr) setBounds({ w: cr.width, h: cr.height });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const cards = surfaces
    ? Object.values(surfaces)
        .filter(
          (d) =>
            (d.mode ?? "embedded") !== "external" && d.mode !== "background",
        )
        .sort((a, b) => (a.z ?? 0) - (b.z ?? 0))
    : [];

  return (
    <div
      ref={layerRef}
      className="surface-layer"
      data-testid={`surface-layer-${view}`}
    >
      {cards.map((d) => (
        <SurfaceCard
          key={d.id}
          view={view}
          descriptor={d}
          bounds={bounds}
          maximizeRect={maximizeRect}
        />
      ))}
    </div>
  );
}

interface SurfaceCardProps {
  view: string;
  descriptor: SurfaceDescriptor;
  bounds: LayerBounds;
  maximizeRect: MaximizeRect | null;
}

const MIN_W = 160;
const MIN_H = 120;

function SurfaceCard({
  view,
  descriptor,
  bounds,
  maximizeRect,
}: SurfaceCardProps) {
  const sendMessage = useWebSocketStore((s) => s.sendMessage);
  // Renderer → tool. Only `clicked` routes anywhere today: it carries a
  // `target` the renderer resolved (a folder row joins its root and name), and
  // goes over the chat WS rather than the surface REST route, because `open`
  // can reach `os_launch`'s ASK and only the WS can put that question to the
  // operator. Geometry events keep their own REST path — they need no gate.
  const dispatch = useCallback(
    (event: OperatorEvent, detail?: Record<string, unknown>) => {
      if (event !== "clicked") return;
      const target = typeof detail?.target === "string" ? detail.target : "";
      if (!target) return;
      sendMessage("surface.open", { target, view });
    },
    [sendMessage, view],
  );
  const move = useSurfacesStore((s) => s.moveSurface);
  const resize = useSurfacesStore((s) => s.resizeSurface);
  const dragSurface = useSurfacesStore((s) => s.dragSurface);
  const dragResize = useSurfacesStore((s) => s.dragResize);
  const close = useSurfacesStore((s) => s.closeSurface);
  const highlight = useSurfacesStore((s) => s.highlights[descriptor.id]);
  const raiseSurface = useSurfacesStore((s) => s.raiseSurface);
  const liveZ = useSurfacesStore((s) => s.liveZ[descriptor.id]);
  const togglePin = useSurfacesStore((s) => s.togglePin);
  const toggleMinimize = useSurfacesStore((s) => s.toggleMinimize);
  const toggleMaximize = useSurfacesStore((s) => s.toggleMaximize);
  const isPinned = useSurfacesStore((s) => s.pinned[descriptor.id] ?? false);
  // Stowed = in the dock. The card stays MOUNTED and is hidden with a class,
  // the way a minimized glass panel is: dropping it from the tree stopped a
  // lane's polling and fired its `unmounted` report, so putting work away
  // quietly ended it.
  const isStowed = useSurfacesStore((s) => s.minimized[descriptor.id] ?? false);
  const isMaximized = useSurfacesStore(
    (s) => s.maximized[descriptor.id] ?? false,
  );

  // Held in place either way: the operator's pin, or the `locked` the backend
  // set on the descriptor. Both read as a pressed pin, so the control says
  // what the card is doing rather than who decided it — but only the
  // operator's own pin is theirs to release. `locked` is server-owned with no
  // client write path, so on a locked card the control is inert and says so,
  // rather than offering an Unlock that flips a flag the lock outranks.
  const heldByAgent = descriptor.locked ?? false;
  const pinned = isPinned || heldByAgent;
  // Drag + resize are inert while pinned or maximized; the card still raises.
  const geoLocked = pinned || isMaximized;
  const Renderer = getRenderer(descriptor.type);
  const known = descriptor.type in RENDERERS;

  // The render half of the canvas → tool channel. Deduped on the last thing
  // sent, because a renderer re-reports on every re-render and the backend
  // only cares when the answer changes.
  const reportedRef = useRef<string | null>(null);
  const report = useCallback<ReportRender>(
    (status, detail = "", controls) => {
      // The dedupe key includes the verbs: a card that mounts and then learns
      // it can be driven (a player API that boots late) is saying something
      // new, and dropping it as a repeat would leave `surface_list` reporting
      // unknown forever.
      const key = `${status}:${detail}:${controls ? controls.join(",") : "?"}`;
      if (reportedRef.current === key) return;
      reportedRef.current = key;
      void reportSurfaceRender(view, descriptor.id, status, detail, controls);
    },
    [view, descriptor.id],
  );

  // Baseline. Child effects run before the parent's, so a renderer that
  // already knows it failed has claimed the slot by the time this runs and
  // `mounted` must not overwrite it — hence the has-anything-been-said check
  // rather than an unconditional report. A renderer that discovers its failure
  // later (a codec that only fails on `onError`) reports over `mounted`, which
  // is correct: that is when it became known.
  useEffect(() => {
    if (!known) {
      report(
        "errored",
        `no renderer for surface type '${descriptor.type}'. The card is showing a JSON dump of its own props.`,
      );
    } else if (reportedRef.current === null) {
      report("mounted");
    }
    return () => {
      void reportSurfaceRender(view, descriptor.id, "unmounted");
    };
  }, [known, report, descriptor.type, descriptor.id, view]);

  // Geometry reads straight from the (live) store — during a drag/resize we
  // push live positions to the store for a responsive card, then commit
  // (emit + persist) on pointer-up.
  const x = descriptor.position.x;
  const y = descriptor.position.y;
  const w = descriptor.size.w;
  const h = descriptor.size.h;

  const lit = useHighlightPulse(highlight);

  const startDrag = (e: React.PointerEvent) => {
    if (geoLocked) return;
    e.preventDefault();
    // Listen on window (not the element) so we capture every move/up across
    // the document without needing setPointerCapture — which can throw on
    // synthetic pointers and would silently abort the drag.
    const ox = e.clientX;
    const oy = e.clientY;
    const bx = descriptor.position.x;
    const by = descriptor.position.y;
    let last = { x: bx, y: by };
    const onMove = (ev: PointerEvent) => {
      last = {
        x: clampX(bx + (ev.clientX - ox), w, bounds),
        y: clampY(by + (ev.clientY - oy), h, bounds),
      };
      dragSurface(view, descriptor.id, last);
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      move(view, descriptor.id, last);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  // 8-way resize: grow the trailing edge (dir +1) or the leading edge (dir −1,
  // which also shifts x/y so the opposite edge stays anchored). Both the live
  // position and size are pushed optimistically, then committed on pointer-up.
  const startResize =
    (dx: -1 | 0 | 1, dy: -1 | 0 | 1) => (e: React.PointerEvent) => {
      if (geoLocked) return;
      e.preventDefault();
      e.stopPropagation();
      const ox = e.clientX;
      const oy = e.clientY;
      const bx = descriptor.position.x;
      const by = descriptor.position.y;
      const bw = descriptor.size.w;
      const bh = descriptor.size.h;
      const right = bx + bw;
      const bottom = by + bh;
      let lastPos = { x: bx, y: by };
      let lastSize = { w: bw, h: bh };
      const onMove = (ev: PointerEvent) => {
        const mx = ev.clientX - ox;
        const my = ev.clientY - oy;
        let nx = bx;
        let ny = by;
        let nw = bw;
        let nh = bh;
        // A trailing edge may not grow past the layer; a leading edge may not
        // drag the card's own origin out of it. Same bound either way, so the
        // card stays whole and grabbable at every edge.
        if (dx === 1)
          nw = Math.max(MIN_W, Math.min(bw + mx, maxW(bx, bounds, MIN_W)));
        else if (dx === -1) {
          nw = Math.max(MIN_W, Math.min(bw - mx, right));
          nx = right - nw;
        }
        if (dy === 1)
          nh = Math.max(MIN_H, Math.min(bh + my, maxH(by, bounds, MIN_H)));
        else if (dy === -1) {
          nh = Math.max(MIN_H, Math.min(bh - my, bottom));
          ny = bottom - nh;
        }
        lastPos = { x: nx, y: ny };
        lastSize = { w: nw, h: nh };
        dragSurface(view, descriptor.id, lastPos);
        dragResize(view, descriptor.id, lastSize);
      };
      const onUp = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        move(view, descriptor.id, lastPos);
        resize(view, descriptor.id, lastSize);
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    };

  // A maximized card fills the stage's maximize rect, which is the same box a
  // maximized view panel fills. Without one measured yet, it falls back to the
  // whole overlay rather than to nothing.
  const geometry = isMaximized
    ? maximizeRect
      ? {
          left: maximizeRect.x,
          top: maximizeRect.y,
          width: maximizeRect.w,
          height: maximizeRect.h,
        }
      : { left: 0, top: 0, width: bounds.w || "100%", height: bounds.h || "100%" }
    : { left: x, top: y, width: w, height: h };

  return (
    <div
      className={`surface-card${lit ? " surface-card--highlight" : ""}${isStowed ? " surface-card--stowed" : ""}${isMaximized ? " surface-card--maximized" : ""}`}
      data-surface-id={descriptor.id}
      data-surface-type={descriptor.type}
      onPointerDown={() => raiseSurface(descriptor.id)}
      style={{ ...geometry, zIndex: liveZ ?? 100 + (descriptor.z ?? 0) }}
    >
      <div className="surface-card__bar" onPointerDown={startDrag}>
        <span className="surface-card__title">
          {descriptor.title ?? descriptor.type}
        </span>
        {/* The same four controls a view panel carries, in the same order. */}
        <div className="surface-card__actions">
          <Hint
            label={
              heldByAgent
                ? "The assistant is holding this one in place"
                : pinned
                  ? "Unlock"
                  : "Hold in place"
            }
          >
            <IconButton
              active={pinned}
              disabled={heldByAgent}
              ariaLabel={
                heldByAgent
                  ? "Held in place by the assistant"
                  : pinned
                    ? "Unlock surface"
                    : "Pin surface in place"
              }
              onClick={() => togglePin(view, descriptor.id)}
              onPointerDown={(e) => e.stopPropagation()}
            >
              <PinIcon filled={pinned} />
            </IconButton>
          </Hint>
          <Hint label="Put away, into the dock">
            <IconButton
              ariaLabel="Minimize surface to the dock"
              onClick={() => toggleMinimize(view, descriptor.id)}
              onPointerDown={(e) => e.stopPropagation()}
            >
              <MinimizeIcon />
            </IconButton>
          </Hint>
          <Hint label={isMaximized ? "Restore" : "Maximize"}>
            <IconButton
              active={isMaximized}
              ariaLabel={isMaximized ? "Restore surface" : "Maximize surface"}
              onClick={() => toggleMaximize(view, descriptor.id)}
              onPointerDown={(e) => e.stopPropagation()}
            >
              <MaximizeIcon on={isMaximized} />
            </IconButton>
          </Hint>
          <CloseButton
            ariaLabel="Close surface"
            onClick={() => close(view, descriptor.id)}
            onPointerDown={(e) => e.stopPropagation()}
          />
        </div>
      </div>
      <div className="surface-card__body">
        <ErrorBoundary
          what={descriptor.title ?? descriptor.type}
          onError={(err) => report("errored", `renderer threw: ${err.message}`)}
        >
          <Renderer
            descriptor={descriptor}
            dispatch={dispatch}
            report={report}
          />
        </ErrorBoundary>
      </div>
      {!geoLocked && (
        <ResizeHandles
          inset
          onResizeStart={(dir) =>
            startResize(RESIZE_VECTOR[dir].dx, RESIZE_VECTOR[dir].dy)
          }
        />
      )}
    </div>
  );
}

// Highlight is a transient pulse: lit immediately on a new pulse, fades
// after a beat unless the pulse is persistent.
function useHighlightPulse(
  pulse: { at: string; persistent: boolean } | undefined,
): boolean {
  const [lit, setLit] = useState(false);
  useEffect(() => {
    if (!pulse) return;
    setLit(true);
    if (pulse.persistent) return;
    const t = window.setTimeout(() => setLit(false), 1500);
    return () => window.clearTimeout(t);
  }, [pulse?.at, pulse?.persistent]);
  return lit;
}
