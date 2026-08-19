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
import { ErrorBoundary } from "../components/common/ErrorBoundary";
import { Hint } from '../components/ui/Hint';

interface SurfaceLayerProps {
  view: string;
}

interface LayerBounds {
  w: number;
  h: number;
}

export function SurfaceLayer({ view }: SurfaceLayerProps) {
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
        <SurfaceCard key={d.id} view={view} descriptor={d} bounds={bounds} />
      ))}
    </div>
  );
}

interface SurfaceCardProps {
  view: string;
  descriptor: SurfaceDescriptor;
  bounds: LayerBounds;
}

const MIN_W = 160;
const MIN_H = 120;

function SurfaceCard({ view, descriptor, bounds }: SurfaceCardProps) {
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
  const toggleMinimize = useSurfacesStore((s) => s.toggleMinimize);
  const toggleMaximize = useSurfacesStore((s) => s.toggleMaximize);
  const rawMinimized = useSurfacesStore(
    (s) => s.minimized[descriptor.id] ?? false,
  );
  const isMaximized = useSurfacesStore(
    (s) => s.maximized[descriptor.id] ?? false,
  );

  const locked = descriptor.locked ?? false;
  // Maximize wins over minimize while active (fills the overlay regardless).
  const isMinimized = rawMinimized && !isMaximized;
  // Drag + resize are inert while locked or maximized; the card still raises.
  const geoLocked = locked || isMaximized;
  const Renderer = getRenderer(descriptor.type);
  const known = descriptor.type in RENDERERS;

  // The render half of the canvas → tool channel. Deduped on the last thing
  // sent, because a renderer re-reports on every re-render and the backend
  // only cares when the answer changes.
  const reportedRef = useRef<string | null>(null);
  const report = useCallback<ReportRender>(
    (status, detail = "") => {
      const key = `${status}:${detail}`;
      if (reportedRef.current === key) return;
      reportedRef.current = key;
      void reportSurfaceRender(view, descriptor.id, status, detail);
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
        `no renderer for surface type '${descriptor.type}' — the card is showing a JSON dump of its own props`,
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
      last = { x: bx + (ev.clientX - ox), y: by + (ev.clientY - oy) };
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
        if (dx === 1) nw = Math.max(MIN_W, bw + mx);
        else if (dx === -1) {
          nw = Math.max(MIN_W, bw - mx);
          nx = right - nw;
        }
        if (dy === 1) nh = Math.max(MIN_H, bh + my);
        else if (dy === -1) {
          nh = Math.max(MIN_H, bh - my);
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

  const geometry = isMaximized
    ? { left: 0, top: 0, width: bounds.w || "100%", height: bounds.h || "100%" }
    : { left: x, top: y, width: w, height: isMinimized ? "auto" : h };

  return (
    <div
      className={`surface-card${lit ? " surface-card--highlight" : ""}${locked ? " surface-card--locked" : ""}${isMinimized ? " surface-card--minimized" : ""}${isMaximized ? " surface-card--maximized" : ""}`}
      data-surface-id={descriptor.id}
      data-surface-type={descriptor.type}
      onPointerDown={() => raiseSurface(descriptor.id)}
      style={{ ...geometry, zIndex: liveZ ?? 100 + (descriptor.z ?? 0) }}
    >
      <div className="surface-card__bar" onPointerDown={startDrag}>
        <span className="surface-card__title">
          {descriptor.title ?? descriptor.type}
        </span>
        {locked ? (
          <span className="surface-card__lock t-meta" aria-label="locked">
            🔒
          </span>
        ) : (
          <div className="surface-card__actions">
            <Hint label={isMaximized ? "Restore" : "Maximize"}>
              <IconButton
                ariaLabel={isMaximized ? "Restore surface" : "Maximize surface"}
                onClick={() => toggleMaximize(view, descriptor.id)}
                onPointerDown={(e) => e.stopPropagation()}
              >
                {isMaximized ? "❐" : "▢"}
              </IconButton>
            </Hint>
            <Hint label={isMinimized ? "Restore" : "Minimize"}>
              <IconButton
                ariaLabel={isMinimized ? "Restore surface" : "Minimize surface"}
                onClick={() => toggleMinimize(view, descriptor.id)}
                onPointerDown={(e) => e.stopPropagation()}
              >
                {isMinimized ? "▲" : "▼"}
              </IconButton>
            </Hint>
            <CloseButton
              ariaLabel="Close surface"
              onClick={() => close(view, descriptor.id)}
              onPointerDown={(e) => e.stopPropagation()}
            />
          </div>
        )}
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
      {!geoLocked && !isMinimized && (
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
