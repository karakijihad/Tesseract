import { useEffect, useRef } from 'react';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import { WebLinksAddon } from '@xterm/addon-web-links';
import { WebglAddon } from '@xterm/addon-webgl';
import { CanvasAddon } from '@xterm/addon-canvas';
import { Unicode11Addon } from '@xterm/addon-unicode11';
import { SearchAddon } from '@xterm/addon-search';
import { useTerminalStore } from '../../stores/terminal';
import { useUIStore } from '../../stores/ui';
import { resolveTheme } from './theme';

interface TerminalInstanceProps {
  paneId: string;
  containerRef: React.RefObject<HTMLDivElement | null>;
}

// F3 (terminal daily-driver 2026-07-05) — debounce window for the
// ResizeObserver callback. Undebounced, a fast panel/split drag fired
// fit() + a full resize round-trip (asyncio.to_thread(setwinsize)) per
// pixel-level resize event; 150ms is the commonly-cited settle window
// for CSS flex/grid transitions (xterm.js community guidance).
export const RESIZE_DEBOUNCE_MS = 150;

function currentThemeConfig() {
  const state = useTerminalStore.getState();
  const name = state.activeThemeName ?? 'mirror';
  return state.config?.themes?.[name] ?? null;
}

export function TerminalInstance({ paneId, containerRef }: TerminalInstanceProps) {
  const initialized = useRef(false);

  useEffect(() => {
    const el = containerRef.current;
    if (!el || initialized.current) return;
    initialized.current = true;

    const term = new Terminal({
      theme: resolveTheme(currentThemeConfig()),
      fontFamily: "'JetBrains Mono', 'Cascadia Code', 'Consolas', monospace",
      fontSize: 14,
      lineHeight: 1.25,
      cursorBlink: true,
      cursorStyle: 'bar',
      // F4 (terminal daily-driver 2026-07-05) — allowTransparency forced
      // alpha-blending on every repaint (worse under the SC-2 glass-panel
      // backdrop-filter); the theme already supplies a solid --bg-void
      // background matching `.wt-canvas`, so opacity was never load-bearing.
      allowTransparency: false,
      scrollback: 5000,
      allowProposedApi: true,
      // Phase 16 — cmd.exe parity (xterm 6.x: windowsMode removed, ConPTY handles EOL)
      convertEol: true,
      altClickMovesCursor: true,
      drawBoldTextInBrightColors: true,
    });

    const fitAddon = new FitAddon();
    const unicode11Addon = new Unicode11Addon();
    const searchAddon = new SearchAddon();
    const webLinksAddon = new WebLinksAddon();

    term.loadAddon(unicode11Addon);
    term.unicode.activeVersion = '11';
    term.loadAddon(fitAddon);
    term.loadAddon(searchAddon);
    term.loadAddon(webLinksAddon);
    term.open(el);

    // F5 (terminal daily-driver 2026-07-05) — renderer fallback chain.
    // Try WebGL first (fastest); on load failure, `onContextLoss`, OR a
    // raw `webglcontextlost` DOM listener (the addon callback is known to
    // sometimes not fire after OS sleep/resume — xterm.js community
    // reports), fall back to the Canvas addon; if that also fails, xterm's
    // built-in DOM renderer is the fallback by omission (no addon loaded).
    let webglAddon: WebglAddon | null = null;
    let canvasAddon: CanvasAddon | null = null;
    let rawLossCanvases: HTMLCanvasElement[] = [];

    const detachRawLossListeners = () => {
      for (const c of rawLossCanvases) c.removeEventListener('webglcontextlost', onRawContextLoss);
      rawLossCanvases = [];
    };

    function loadCanvasFallback(reason: string): void {
      if (canvasAddon) return; // already fell back once
      try {
        canvasAddon = new CanvasAddon();
        term.loadAddon(canvasAddon);
        console.debug(`[terminal] renderer fallback: webgl -> canvas (${reason})`);
      } catch {
        canvasAddon = null;
        console.debug(`[terminal] renderer fallback: canvas -> dom (canvas load failed after ${reason})`);
      }
    }

    function handleWebglLoss(reason: string): void {
      detachRawLossListeners();
      if (webglAddon) {
        try {
          webglAddon.dispose();
        } catch {
          // context already gone — nothing to dispose
        }
        webglAddon = null;
      }
      loadCanvasFallback(reason);
    }

    function onRawContextLoss(): void {
      handleWebglLoss('webglcontextlost DOM event');
    }

    try {
      webglAddon = new WebglAddon();
      webglAddon.onContextLoss(() => handleWebglLoss('onContextLoss callback'));
      term.loadAddon(webglAddon);
      // Second detection path — post-sleep/resume context loss can skip
      // the addon's own onContextLoss callback.
      rawLossCanvases = Array.from(el.querySelectorAll('canvas'));
      for (const c of rawLossCanvases) c.addEventListener('webglcontextlost', onRawContextLoss);
    } catch {
      webglAddon = null;
      console.debug('[terminal] renderer fallback: webgl load failed');
      loadCanvasFallback('webgl load failed');
    }

    // ViewRouter mounts every view hidden at boot, so this effect may
    // run with the container at 0×0. ResizeObserver doesn't refire on
    // display:none→flex, so we poll rAF until the pane is in layout
    // before fitting — otherwise xterm records 0 cols × 0 rows and the
    // tab stays blank until torn down and rebuilt.
    let fitRafId = 0;
    const fitWhenVisible = () => {
      if (el.clientWidth > 0 && el.clientHeight > 0) {
        fitAddon.fit();
        useTerminalStore.getState().sendResize(paneId, term.cols, term.rows);
        term.focus();
        return;
      }
      fitRafId = requestAnimationFrame(fitWhenVisible);
    };
    fitRafId = requestAnimationFrame(fitWhenVisible);

    // Phase 16 — clipboard: Ctrl+V paste, Ctrl+C copy (when selection active)
    term.attachCustomKeyEventHandler((ev) => {
      if (ev.type !== 'keydown') return true;
      // Ctrl+V → paste from clipboard into PTY
      if (ev.ctrlKey && ev.key === 'v') {
        navigator.clipboard.readText().then((text) => {
          if (text) useTerminalStore.getState().sendKeystroke(paneId, text);
        }).catch(() => {});
        return false; // prevent xterm default
      }
      // Ctrl+C with active selection → copy to clipboard (don't send ^C)
      if (ev.ctrlKey && ev.key === 'c' && term.hasSelection()) {
        navigator.clipboard.writeText(term.getSelection()).catch(() => {});
        term.clearSelection();
        return false;
      }
      return true; // all other keys pass through to xterm
    });

    useTerminalStore.getState().attachTerminal(paneId, term, searchAddon);

    const dataDispose = term.onData((data) => {
      useTerminalStore.getState().sendKeystroke(paneId, data);
    });

    let resizeTimer: ReturnType<typeof setTimeout> | null = null;
    const ro = new ResizeObserver(() => {
      if (resizeTimer !== null) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        resizeTimer = null;
        // 0×0 guard — a resize can fire mid-transition (panel collapse,
        // split drag past the edge) before real layout settles.
        if (el.clientWidth <= 0 || el.clientHeight <= 0) return;
        fitAddon.fit();
        useTerminalStore.getState().sendResize(paneId, term.cols, term.rows);
      }, RESIZE_DEBOUNCE_MS);
    });
    ro.observe(el);

    // Force a full redraw whenever the Terminal view becomes visible again —
    // covers both tab switches inside Mirror (UIStore.view → 'terminal') and
    // browser tab background → foreground (document.visibilitychange).
    // Belt-and-suspenders with the `view-pane--keep-mounted` CSS rule, which
    // already keeps the WebGL canvas painting; `refresh + fit` guarantees the
    // last buffered scrollback lines are fully rendered after any pause.
    const refreshIfVisible = () => {
      if (el.clientWidth === 0 || el.clientHeight === 0) return;
      fitAddon.fit();
      term.refresh(0, term.rows - 1);
    };
    const unsubView = useUIStore.subscribe((state, prev) => {
      if (state.view === 'terminal' && prev.view !== 'terminal') {
        requestAnimationFrame(refreshIfVisible);
      }
    });
    const onDocVisibility = () => {
      if (document.visibilityState === 'visible') {
        requestAnimationFrame(refreshIfVisible);
      }
    };
    document.addEventListener('visibilitychange', onDocVisibility);

    // Re-resolve theme when entity accent or any CSS var changes — matters
    // for token-based themes (e.g., `mirror`) so xterm tracks --accent live.
    const themeObserver = new MutationObserver(() => {
      term.options.theme = resolveTheme(currentThemeConfig());
    });
    themeObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['style', 'data-entity-color'],
    });

    return () => {
      initialized.current = false;
      if (fitRafId) cancelAnimationFrame(fitRafId);
      if (resizeTimer !== null) clearTimeout(resizeTimer);
      dataDispose.dispose();
      ro.disconnect();
      themeObserver.disconnect();
      unsubView();
      document.removeEventListener('visibilitychange', onDocVisibility);
      useTerminalStore.getState().detachTerminal(paneId);
      detachRawLossListeners();
      webglAddon?.dispose();
      canvasAddon?.dispose();
      term.dispose();
    };
  }, [paneId, containerRef]);

  return null;
}
