// Y-3 — Pulse event stream as a Surface Protocol card. The header controls
// (cap selector, errors-only, clear) + the scrolling event list from the old
// PulseView. Filter state is read from the pulse store, shared with the
// PulseFilterRenderer card.

import { useEffect, useMemo, useRef, useState } from 'react';

import { linkifyText } from '../../lib/linkify';
import { usePulseStore, type PulseCapValue, type PulseEntry } from '../../stores/pulse';
import type { RendererProps } from './index';

const CAP_OPTIONS: { value: PulseCapValue; label: string }[] = [
  { value: 100, label: '100' },
  { value: 500, label: '500' },
  { value: 'all', label: 'All' },
];

function formatTs(ts: string): string {
  try {
    return new Date(ts).toLocaleTimeString([], {
      hour12: false,
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  } catch {
    return '';
  }
}

function Row({ entry }: { entry: PulseEntry }) {
  return (
    <div
      className={`pulse-event${entry.severity === 'bad' ? ' highlight' : ''}`}
      data-severity={entry.severity}
    >
      <span className="ev-ts">{formatTs(entry.ts)}</span>
      <span className={`ev-tag ${entry.tag}`}>{entry.tag}</span>
      <span className="ev-msg">{linkifyText(entry.label)}</span>
    </div>
  );
}

export function PulseStreamRenderer(_props: RendererProps) {
  const entries = usePulseStore((s) => s.entries);
  const clear = usePulseStore((s) => s.clear);
  const cap = usePulseStore((s) => s.cap);
  const setCap = usePulseStore((s) => s.setCap);
  const enabledTags = usePulseStore((s) => s.enabledTags);
  const errorsOnly = usePulseStore((s) => s.errorsOnly);
  const setErrorsOnly = usePulseStore((s) => s.setErrorsOnly);
  const resetFilter = usePulseStore((s) => s.resetFilter);
  const capLabel = cap === 'all' ? '∞' : String(cap);

  const filtered = useMemo(() => {
    let rows = entries;
    if (errorsOnly) rows = rows.filter((e) => e.severity === 'bad');
    if (enabledTags !== null) rows = rows.filter((e) => enabledTags.has(e.tag));
    return rows;
  }, [entries, enabledTags, errorsOnly]);

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [locked, setLocked] = useState(false);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el || locked) return;
    el.scrollTop = 0;
  }, [filtered, locked]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    setLocked(el.scrollTop > 8);
  };

  const filterActive = enabledTags !== null || errorsOnly;
  const errorCount = useMemo(
    () => entries.reduce((n, e) => (e.severity === 'bad' ? n + 1 : n), 0),
    [entries],
  );

  return (
    <div className="pulse-view pulse-view--card">
      <div className="pulse-header">
        <span className="pulse-header-meta">
          {filtered.length}{filterActive ? ` / ${entries.length}` : ''} / {capLabel}{locked ? ' · locked' : ''}
        </span>
        <div className="pulse-cap-selector" role="group" aria-label="Pulse retention cap">
          {CAP_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              type="button"
              className={`pulse-cap-btn${cap === opt.value ? ' is-active' : ''}`}
              onClick={() => setCap(opt.value)}
              aria-pressed={cap === opt.value}
              data-cap={opt.value}
            >
              {opt.label}
            </button>
          ))}
          {cap === 'all' && (
            <span className="pulse-cap-warn t-meta" title="No cap — long sessions may slow the panel">
              unbounded — may affect performance
            </span>
          )}
        </div>
        <div className="pulse-header-actions">
          <button
            type="button"
            onClick={() => setErrorsOnly(!errorsOnly)}
            className={`pulse-header-btn${errorsOnly ? ' is-active' : ''}`}
            aria-pressed={errorsOnly}
            title="Show only error rows from the Mirror backend"
          >
            errors{errorCount > 0 ? ` (${errorCount})` : ''}
          </button>
          {filterActive && (
            <button type="button" onClick={resetFilter} className="pulse-header-btn">
              clear filter
            </button>
          )}
          {entries.length > 0 && (
            <button type="button" onClick={clear} className="pulse-header-btn">
              clear
            </button>
          )}
        </div>
      </div>
      <div className="pulse-stream" ref={scrollRef} onScroll={onScroll}>
        {filtered.length === 0 ? (
          <div className="pulse-empty">
            {filterActive ? 'No events match filter' : 'Waiting for events…'}
          </div>
        ) : (
          filtered.map((e) => <Row key={e.id} entry={e} />)
        )}
      </div>
    </div>
  );
}
