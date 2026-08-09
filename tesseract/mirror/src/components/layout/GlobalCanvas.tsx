import { useEffect, useRef } from 'react';
import { useOrbDockStore } from '../../stores/orbDock';
import { useOrbVisibilityStore } from '../../stores/orbVisibility';
import { ParticleSystem } from '../../lib/entity/ParticleSystem';
import { AmbientHaze } from '../../lib/entity/AmbientHaze';
import { EntityController } from '../../lib/entity/EntityController';
import type { EntityMode } from '../../lib/entity/EntityController';
import { setController } from '../../lib/entity/registry';
import { useEntityStore } from '../../stores/entity';

// SC-1 — the spatial cockpit shows the orb full-screen as the fixed
// centerpiece at all times (the orb stays clamped between the viewport top and
// the `.cockpit-hud` bar exactly as it was on the immersive OrbView). The old
// per-view corner-dock is retired with the ViewRouter. The corner-mode
// positioning below is dead while this returns 'full'; kept until SC-3 settles
// the rails so a revert stays cheap.
function useEntityMode(): EntityMode {
  return 'full';
}

export function GlobalCanvas() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const controllerRef = useRef<EntityController | null>(null);
  const psRef = useRef<ParticleSystem | null>(null);
  const mode = useEntityMode();
  const dockEl = useOrbDockStore(s => s.dockEl);
  const orbVisible = useOrbVisibilityStore(s => s.visible);
  // Hide the canvas only when we'd be drawing it without a target
  // (corner-mode and the dock element isn't mounted yet, e.g. transient
  // unmount during view switch), or when the operator has hidden it via the
  // HUD toggle. Full mode always shows otherwise. This kills the
  // center-floating fallback that triggered when corner-mode lost its
  // dockEl mid-render.
  const hidden = (mode === 'corner' && !dockEl) || !orbVisible;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    if (controllerRef.current) return;

    const ps = new ParticleSystem();
    const haze = new AmbientHaze();
    const controller = new EntityController();

    const w = canvas.clientWidth || window.innerWidth;
    const h = canvas.clientHeight || window.innerHeight;
    canvas.width = w;
    canvas.height = h;

    ps.init(canvas, 3500);
    haze.init(ps.getScene());
    controller.init(ps, haze, canvas);
    // Single source of truth for the orb's hue: read the CSS accent token
    // (tokens.css `--accent-hsl`) and push it through the entity store so
    // the WebGL orb matches the UI palette. Without this the orb keeps its
    // hardcoded default and drifts from the theme on a recolor.
    const cssAccent = getComputedStyle(document.documentElement)
      .getPropertyValue('--accent-hsl')
      .trim();
    if (cssAccent) useEntityStore.getState().setAccentHsl(cssAccent);
    controllerRef.current = controller;
    psRef.current = ps;
    setController(controller);

    if (import.meta.env.DEV) {
      (window as unknown as { __entityController: EntityController }).__entityController = controller;
    }

    const resizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect;
        if (width > 0 && height > 0) {
          canvas.width = width;
          canvas.height = height;
          ps.resize(width, height);
        }
      }
    });
    resizeObserver.observe(canvas.parentElement ?? document.body);

    return () => {
      resizeObserver.disconnect();
      setController(null);
      controller.dispose();
      controllerRef.current = null;
      psRef.current = null;
    };
  }, []);

  // Render-loop lifecycle: a hidden canvas (`display:none`) stops compositing
  // but NOT the rAF loop — the particle sim + WebGL draws keep burning
  // GPU/CPU for an invisible orb. Pause the controller whenever the orb is
  // hidden (HUD toggle) or the tab is backgrounded; resume restores the loop
  // with identical quality (nothing about the visible rendering changes).
  useEffect(() => {
    const sync = () => {
      const ctrl = controllerRef.current;
      if (!ctrl) return;
      if (hidden || document.hidden) ctrl.pause();
      else ctrl.resume();
    };
    sync();
    document.addEventListener('visibilitychange', sync);
    return () => document.removeEventListener('visibilitychange', sync);
  }, [hidden]);

  // Mode switch: instant reposition (no fade — fade causes race conditions on long queries)
  useEffect(() => {
    const canvas = canvasRef.current;
    const ctrl = controllerRef.current;
    const ps = psRef.current;
    if (!canvas || !ctrl || !ps) return;

    ctrl.setMode(mode);
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    if (w > 0 && h > 0) {
      canvas.width = w;
      canvas.height = h;
      ps.resize(w, h);
    }
  }, [mode]);

  // Corner-mode dock tracking (F2 §5h): when not on the home view, the orb
  // sits inside the right-panel `.orb-dock` slot. We don't move the canvas
  // element (single WebGL context — moving it would lose the GL state); we
  // reposition + resize it inline via ResizeObserver. When mode flips back
  // to full we clear the inline styles so the `.global-canvas.full` rule
  // applies and the previous mode-switch effect resizes the buffer.
  //
  // Resizing both the CSS box AND the canvas backing buffer (canvas.width
  // / canvas.height + ps.resize) is load-bearing: dropping the buffer
  // resize leaves the orb rendering at its full-screen pixel dimensions
  // and CSS-squishing it down, which produces a pixelated, distorted blob
  // instead of the sharp particle field.
  useEffect(() => {
    const canvas = canvasRef.current;
    const ps = psRef.current;
    if (!canvas) return;

    if (mode === 'corner' && !dockEl) {
      // Corner mode without a mounted dockEl — visually hidden upstream
      // via the `hidden` flag; nothing to position. Bail before the
      // full-screen sync runs (this was the source of the center-floating
      // orb bug on tabs that briefly lost their dock during transitions).
      return;
    }

    if (mode === 'full') {
      // Full mode locks the canvas between two cockpit reference lines —
      // top of viewport (y=0) and top of `.cockpit-hud` (operator request:
      // orb stays centered between the two lines as either moves). Without
      // this clamp, the orb is centered in the full viewport and visually
      // pulls down behind the HUD bar.
      const hudEl = document.querySelector<HTMLElement>('.cockpit-hud');
      let lastTargetH = -1;

      const syncFull = () => {
        const hudRect = hudEl?.getBoundingClientRect();
        const targetH = hudRect && hudRect.top > 0
          ? Math.round(hudRect.top)
          : window.innerHeight;
        if (targetH !== lastTargetH) {
          canvas.style.top = '0';
          canvas.style.left = '0';
          canvas.style.width = '100vw';
          canvas.style.height = `${targetH}px`;
          lastTargetH = targetH;
        }
        const w = canvas.clientWidth;
        const h = canvas.clientHeight;
        if (w > 0 && h > 0 && (canvas.width !== w || canvas.height !== h)) {
          canvas.width = w;
          canvas.height = h;
          if (ps) ps.resize(w, h);
        }
      };

      requestAnimationFrame(syncFull);

      if (!hudEl) {
        // The orb falls back to the full viewport height (no `.cockpit-hud`
        // clamp). In the spatial cockpit the HUD is always mounted (CockpitStage
        // is committed before this effect), so reaching here means a DOM-order
        // regression — warn in dev so the silent overlap is observable.
        if (import.meta.env.DEV) {
          console.warn(
            '[GlobalCanvas] full-mode clamp ran without a `.cockpit-hud` element; orb uses full viewport height.',
          );
        }
        const onResize = () => syncFull();
        window.addEventListener('resize', onResize);
        return () => window.removeEventListener('resize', onResize);
      }

      const ro = new ResizeObserver(syncFull);
      ro.observe(hudEl);
      ro.observe(document.documentElement);
      const onResize = () => syncFull();
      window.addEventListener('resize', onResize);
      return () => {
        ro.disconnect();
        window.removeEventListener('resize', onResize);
      };
    }

    // Dock = 150X150 square, orb canvas = 200×200 centered inside. The
    // 40px combined margin is visible space between the dock edge and
    // the canvas — matches the original bottom-of-panel size (200). The
    // sphere is then grown inside the canvas by pulling the camera
    // closer (ParticleSystem.ts: position.z = 4), not by enlarging the
    // canvas — avoids touching particle distribution.
    const ORB_SIZE = 230;
    let lastLeft = 0;
    let lastTop = 0;
    let inited = false;

    // Narrowed for TS: the `!dockEl` guard above plus the `mode==='full'`
    // branch means we only reach here with a real dockEl. Capture it
    // so the inner closures don't need the non-null assertion.
    if (!dockEl) return;
    const dock: HTMLElement = dockEl;

    const sync = () => {
      const rect = dock.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) return;
      const cx = rect.left + rect.width / 2;
      const cy = rect.top + rect.height / 2;
      const left = Math.round(cx - ORB_SIZE / 2);
      const top = Math.round(cy - ORB_SIZE / 2);
      // Skip only when nothing requires a re-sync: position unchanged AND
      // backing buffer is already the correct ORB_SIZE square. A stale
      // non-square buffer (e.g. left over from full-mode) still needs the
      // body to run so we re-size canvas.width/height and the particle
      // system — otherwise CSS squishes the full-screen buffer into the
      // dock slot and the orb looks pixelated.
      if (
        inited &&
        left === lastLeft &&
        top === lastTop &&
        canvas.width === ORB_SIZE &&
        canvas.height === ORB_SIZE
      ) return;
      inited = true;
      lastLeft = left;
      lastTop = top;
      canvas.style.top = `${top}px`;
      canvas.style.left = `${left}px`;
      canvas.style.width = `${ORB_SIZE}px`;
      canvas.style.height = `${ORB_SIZE}px`;
      // Backing buffer uses raw CSS pixels — matches the convention of
      // the mount and mode-switch effects above. An earlier DPR-multiplied
      // buffer produced a faded, blurred orb because ParticleSystem's
      // projection was sized to CSS pixels while the buffer was 2×.
      if (canvas.width !== ORB_SIZE || canvas.height !== ORB_SIZE) {
        canvas.width = ORB_SIZE;
        canvas.height = ORB_SIZE;
        if (ps) ps.resize(ORB_SIZE, ORB_SIZE);
      }
    };

    sync();
    const ro = new ResizeObserver(sync);
    ro.observe(dock);
    ro.observe(document.documentElement);
    window.addEventListener('scroll', sync, { passive: true });
    return () => {
      ro.disconnect();
      window.removeEventListener('scroll', sync);
    };
  }, [mode, dockEl]);

  return (
    <canvas
      ref={canvasRef}
      className={`global-canvas ${mode}${hidden ? ' hidden' : ''}`}
      style={hidden ? { display: 'none' } : undefined}
      aria-hidden="true"
    />
  );
}
