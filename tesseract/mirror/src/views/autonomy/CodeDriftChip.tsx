import { useEffect, useRef, useState } from 'react';

import { BACKEND_BASE } from '../../lib/endpoints';
import {
  deriveDriftSelection,
  useCodeDriftStore,
  type CodeDriftStatus,
  type DriftEvent,
} from '../../stores/codeDrift';
import { useToastStore } from '../../stores/toasts';
import { useWebSocketStore } from '../../stores/websocket';

// Persistent header chip + drift history panel.
//
// The chip face summarises the worst non-ignored drift state. Clicking
// expands a dropdown with: actions row (Restart if backend drift / Ignore
// or Un-ignore / Clear history) and a scrollable list of every drift
// event the backend has reported this session — timestamp, classification,
// path count, and a sample of paths. Toasts are transient nudges; this
// panel is the durable record.

interface ChipFace {
  label: string;
  dotClass: string;
  title: string;
}

function _faceFor(status: CodeDriftStatus, ignored: boolean, pathCount: number): ChipFace {
  if (status === 'ok' || ignored) {
    return {
      label: ignored ? 'code ignored' : 'code ok',
      dotClass: 'code-drift-chip__dot--ok',
      title: ignored
        ? 'Drift detected but ignored. A new drift event will re-assert this chip.'
        : 'No source drift detected since boot.',
    };
  }
  if (status === 'frontend_only') {
    return {
      label: `frontend drift · ${pathCount}`,
      dotClass: 'code-drift-chip__dot--frontend',
      title: 'Frontend files edited. Reload the page to see them.',
    };
  }
  return {
    label: `restart needed · ${pathCount}`,
    dotClass: 'code-drift-chip__dot--restart',
    title: 'Backend Python edited since boot. The running process holds old bytecode.',
  };
}

function _fmtTimeAgo(iso: string): string {
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return '—';
  const seconds = Math.max(0, (Date.now() - parsed) / 1000);
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function _classChipFor(classification: DriftEvent['classification']): string {
  return classification === 'restart_required'
    ? 'code-drift-event__class code-drift-event__class--restart'
    : 'code-drift-event__class code-drift-event__class--frontend';
}

export function CodeDriftChip(): React.ReactElement {
  const events = useCodeDriftStore((s) => s.events);
  const ignored = useCodeDriftStore((s) => s.ignored);
  const ignore = useCodeDriftStore((s) => s.ignore);
  const reset = useCodeDriftStore((s) => s.reset);
  const clearHistory = useCodeDriftStore((s) => s.clearHistory);
  const { status, pathCount, headSha, hasRestartRequired } = deriveDriftSelection(events);
  const [menuOpen, setMenuOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const rootRef = useRef<HTMLSpanElement | null>(null);

  // Close on outside click — the chip lives in a busy header, so a
  // click-away dismiss is the only natural close gesture.
  useEffect(() => {
    if (!menuOpen) return;
    const onDocClick = (e: MouseEvent) => {
      const root = rootRef.current;
      if (root && e.target instanceof Node && !root.contains(e.target)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, [menuOpen]);

  const face = _faceFor(status, ignored, pathCount);
  const showRestart = hasRestartRequired && !ignored;

  const onIgnore = () => {
    ignore();
  };

  const onUnignore = () => {
    reset();
    setMenuOpen(false);
  };

  const onClear = () => {
    clearHistory();
    setMenuOpen(false);
  };

  const onRestart = async () => {
    if (pending) return;
    // Restart endpoint accepts localhost without session — Mirror binds
    // 127.0.0.1 only. Pass session_id when we have one for audit clarity
    // but don't gate the click on it (cold-boot windows have no session
    // yet, and that's exactly when code-drift restart is most needed).
    const sessionId = useWebSocketStore.getState().sessionId;
    setPending(true);
    try {
      const body: Record<string, unknown> = {
        head_sha: headSha,
        reason: `operator clicked code-drift chip (${pathCount} backend path(s) changed)`,
      };
      if (sessionId) body.session_id = sessionId;
      const resp = await fetch(`${BACKEND_BASE}/api/runtime/restart_for_code_drift`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        useToastStore.getState().push(`Restart failed: HTTP ${resp.status}`, 'error');
        setPending(false);
        return;
      }
      // The WS will disconnect within ~500ms; the reconnect loop will
      // re-hydrate the chip on the new backend's first envelope.
      setMenuOpen(false);
    } catch (e) {
      useToastStore.getState().push(
        `Restart failed: ${e instanceof Error ? e.message : String(e)}`,
        'error',
      );
      setPending(false);
    }
  };

  return (
    <span className="code-drift-chip" data-testid="code-drift-chip" ref={rootRef}>
      <button
        type="button"
        className="code-drift-chip__button t-meta"
        onClick={() => setMenuOpen((v) => !v)}
        title={face.title}
        aria-haspopup="menu"
        aria-expanded={menuOpen}
      >
        <span className={`code-drift-chip__dot ${face.dotClass}`} aria-hidden="true" />
        {face.label}
        {events.length > 0 && (
          <span className="code-drift-chip__count t-meta" aria-label={`${events.length} drift events`}>
            ({events.length})
          </span>
        )}
      </button>
      {menuOpen && (
        <div className="code-drift-chip__menu" role="menu">
          <div className="code-drift-chip__actions">
            {showRestart && (
              <button
                type="button"
                className="code-drift-chip__action"
                onClick={onRestart}
                disabled={pending}
                role="menuitem"
              >
                {pending ? 'Restarting…' : 'Restart backend'}
              </button>
            )}
            {events.length > 0 && !ignored && (
              <button
                type="button"
                className="code-drift-chip__action"
                onClick={onIgnore}
                role="menuitem"
              >
                Ignore
              </button>
            )}
            {ignored && (
              <button
                type="button"
                className="code-drift-chip__action"
                onClick={onUnignore}
                role="menuitem"
              >
                Un-ignore
              </button>
            )}
            {events.length > 0 && (
              <button
                type="button"
                className="code-drift-chip__action code-drift-chip__action--secondary"
                onClick={onClear}
                role="menuitem"
              >
                Clear history
              </button>
            )}
          </div>

          {events.length === 0 ? (
            <p className="code-drift-chip__hint t-meta">No drift events.</p>
          ) : (
            <ul className="code-drift-chip__list" role="presentation">
              {events.map((ev) => {
                const sample = ev.paths.slice(0, 3).join(', ');
                const more = ev.paths.length > 3 ? ` (+${ev.paths.length - 3})` : '';
                const sha = ev.headSha ? ev.headSha.slice(0, 8) : '';
                return (
                  <li key={ev.id} className="code-drift-event">
                    <div className="code-drift-event__head">
                      <span className={_classChipFor(ev.classification)}>
                        {ev.classification === 'restart_required' ? 'restart' : 'frontend'}
                      </span>
                      <span className="t-meta">
                        {ev.pathCount} file{ev.pathCount === 1 ? '' : 's'}
                      </span>
                      <span className="code-drift-event__when t-meta">
                        {_fmtTimeAgo(ev.detectedAt)}
                      </span>
                    </div>
                    {sample && (
                      <div className="code-drift-event__paths t-meta">
                        {sample}{more}
                      </div>
                    )}
                    {sha && (
                      <div className="code-drift-event__sha t-meta">
                        HEAD {sha}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </span>
  );
}
