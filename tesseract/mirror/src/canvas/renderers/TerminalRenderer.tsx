// Y-2 — terminal surface. Reuses the xterm.js plumbing (same dep + fit
// addon as the Terminal view's TerminalInstance) to render a read-only
// terminal pane. `props.buffer` is the captured output the tool supplied;
// live PTY attachment to a bound controller lane is wired in CV-1 (this
// renderer already shows the bound session id when present).
//
// The xterm mount is guarded so a headless / jsdom environment (no real
// canvas) degrades to a <pre> fallback instead of crashing the canvas.

import { useEffect, useRef, useState } from 'react';

import type { RendererProps } from './index';

export function TerminalRenderer({ descriptor }: RendererProps) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const [degraded, setDegraded] = useState(false);
  const buffer = String(descriptor.props?.buffer ?? '');
  const bound = descriptor.bound_session;

  useEffect(() => {
    const el = hostRef.current;
    if (!el) return;
    let term: { dispose: () => void } | null = null;
    let cancelled = false;
    void (async () => {
      try {
        const [{ Terminal }, { FitAddon }] = await Promise.all([
          import('@xterm/xterm'),
          import('@xterm/addon-fit'),
        ]);
        if (cancelled) return;
        const t = new Terminal({
          fontFamily: "'JetBrains Mono', 'Consolas', monospace",
          fontSize: 13,
          disableStdin: true,
          convertEol: true,
          allowTransparency: true,
          scrollback: 5000,
        });
        const fit = new FitAddon();
        t.loadAddon(fit);
        t.open(el);
        try {
          fit.fit();
        } catch {
          /* fit needs layout; non-fatal */
        }
        if (buffer) t.write(buffer);
        term = t;
      } catch {
        if (!cancelled) setDegraded(true);
      }
    })();
    return () => {
      cancelled = true;
      term?.dispose();
    };
  }, [buffer]);

  return (
    <div className="surface-terminal">
      {bound ? (
        <div className="surface-terminal__bound t-meta">
          ● {bound.kind}:{bound.id}
        </div>
      ) : null}
      {degraded ? (
        <pre className="surface-terminal__fallback">{buffer || '(no output)'}</pre>
      ) : (
        <div className="surface-terminal__host" ref={hostRef} />
      )}
    </div>
  );
}
