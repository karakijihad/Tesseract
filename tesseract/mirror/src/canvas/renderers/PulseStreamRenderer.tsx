// Y-3 — Pulse event stream as a Surface Protocol card. The header controls
// (cap selector, errors-only, clear) + the scrolling event list from the old
// PulseView. Filter state is read from the pulse store, shared with the
// PulseFilterRenderer card.

import { Button } from '../../components/common/Button';
import { useEffect, useMemo, useRef, useState } from 'react';

import { linkifyText } from '../../lib/linkify';
import { usePulseStore, type PulseCapValue, type PulseEntry } from '../../stores/pulse';
import type { RendererProps } from './index';
import { Hint } from '../../components/ui/Hint';
import { Segmented } from '../../components/common/Segmented';
import { ViewHeader } from '../../components/common/ViewHeader';

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
      <ViewHeader
        meta={
          <>
            {filtered.length}{filterActive ? ` / ${entries.length}` : ''} / {capLabel}{locked ? ' · locked' : ''}
          </>
        }
        actions={
          <>
            <div className="pulse-cap-selector">
              <Segmented
                items={CAP_OPTIONS.map((o) => ({ key: o.value, label: o.label }))}
                value={cap}
                onSelect={setCap}
                label="Pulse retention cap"
              />
              {cap === 'all' && (
                <Hint label="No cap — long sessions may slow the panel">
                  <span className="pulse-cap-warn t-meta">
                    unbounded — may affect performance
                  </span>
                </Hint>
              )}
            </div>
            <Hint label="Show only error rows from the Mirror backend">
              <Button
                onClick={() => setErrorsOnly(!errorsOnly)}
                active={errorsOnly}
                ariaLabel="show errors only"
              >
                errors{errorCount > 0 ? ` (${errorCount})` : ''}
              </Button>
            </Hint>
            {filterActive && (
              <Button onClick={resetFilter} ariaLabel="clear filter">
                clear filter
              </Button>
            )}
            {entries.length > 0 && (
              <Button onClick={clear} ariaLabel="clear">
                clear
              </Button>
            )}
          </>
        }
      />
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
