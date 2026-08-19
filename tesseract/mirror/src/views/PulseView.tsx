import { useEffect, useMemo, useRef, useState } from 'react';

import { Button } from '../components/common/Button';
import { Block } from '../components/common/Block';
import { Note } from '../components/common/Note';
import { Segmented } from '../components/common/Segmented';
import { RailView, type RailGroup } from '../components/common/RailView';
import { linkifyText } from '../lib/linkify';
import type { PulseTag } from '../lib/types';
import { Hint } from '../components/ui/Hint';
import {
  ALL_PULSE_TAGS,
  usePulseStore,
  type PulseCapValue,
  type PulseEntry,
} from '../stores/pulse';

const CAP_OPTIONS: { value: PulseCapValue; label: string }[] = [
  { value: 100, label: '100' },
  { value: 500, label: '500' },
  { value: 'all', label: 'All' },
];

const TAG_HINTS: Record<PulseTag, string> = {
  triage:  'early session setup, connection + catch-up events',
  tool:    'tool calls: stream_tool_call_end, tool_auto, sandbox',
  memory:  'memory_save / memory_update / memory_forget / memory_search, memory_suggestion',
  agent:   'cli_start / cli_output / cli_end (delegate_*), invoke_agent',
  model:   'model_selected — chat_brain and observer_agent model resolution',
  system:  'session_created / saved / loaded / reset / compact, soul_updated',
  chat:    'stream_text + generic loop traffic not otherwise tagged',
  perm:    'tool_ask, tool_approved, tool_denied, tool_denied_hard',
  route:   'mode_changed (security mode flips)',
  loop:    'loop_start, loop_end, stream_stop — turn lifecycle',
  bg:      'background category: observer_result / observer_unavailable / compaction_trigger',
  other:   'uncategorized: entity signals (filtered at push), terminal bypass, planning, unmapped',
};

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

// SC-0 — Pulse reverts to whole-view rendering (the Y-3 canvas-shell split into
// pulse-stream / pulse-filters cards is superseded by the spatial-cockpit panel
// model). The header + filter chips + scrolling stream render together; SC-2's
// panel manager hosts this component unchanged inside a glass panel. Filter
// state stays in the pulse store (the genuinely-useful Y-3 backend change).
export function PulseView() {
  const entries = usePulseStore((s) => s.entries);
  const clear = usePulseStore((s) => s.clear);
  const cap = usePulseStore((s) => s.cap);
  const setCap = usePulseStore((s) => s.setCap);
  const enabledTags = usePulseStore((s) => s.enabledTags);
  const errorsOnly = usePulseStore((s) => s.errorsOnly);
  const toggleTag = usePulseStore((s) => s.toggleTag);
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

  const isTagEnabled = (tag: PulseTag) => enabledTags === null || enabledTags.has(tag);
  const filterActive = enabledTags !== null || errorsOnly;
  const errorCount = useMemo(
    () => entries.reduce((n, e) => (e.severity === 'bad' ? n + 1 : n), 0),
    [entries],
  );

  const tagCounts = useMemo(() => {
    const counts = new Map<PulseTag, number>();
    for (const e of entries) counts.set(e.tag, (counts.get(e.tag) ?? 0) + 1);
    return counts;
  }, [entries]);

  const groups: RailGroup[] = [
    {
      label: 'Feed',
      sections: [
        {
          key: 'stream',
          label: 'Stream',
          meta: `${filtered.length}${filterActive ? ` / ${entries.length}` : ''} / ${capLabel}${locked ? ' · locked' : ''}`,
          actions: (
            <>
              <Hint label="Show only error rows from the Mirror backend">
                <Button
                  onClick={() => setErrorsOnly(!errorsOnly)}
                  active={errorsOnly}
                  ariaLabel="show errors only"
                >
                  errors{errorCount > 0 ? ` (${errorCount})` : ''}
                </Button>
              </Hint>
              {entries.length > 0 && (
                <Button onClick={clear} ariaLabel="clear the event stream">
                  clear
                </Button>
              )}
            </>
          ),
          render: () => (
            <div className="pulse-stream" ref={scrollRef} onScroll={onScroll}>
              {filtered.length === 0 ? (
                <div className="pulse-empty">
                  {filterActive ? 'No events match filter' : 'Waiting for events…'}
                </div>
              ) : (
                filtered.map((e) => <Row key={e.id} entry={e} />)
              )}
            </div>
          ),
        },
        {
          key: 'retention',
          label: 'Retention',
          meta: `keeping ${capLabel}`,
          render: () => (
            <Block title="How much to keep" meta={`${entries.length} held`}>
              <Segmented
                items={CAP_OPTIONS.map((o) => ({ key: o.value, label: o.label }))}
                value={cap}
                onSelect={setCap}
                label="Pulse retention cap"
                className="pulse-cap-selector"
              />
              {cap === 'all' && (
                <Note tone="warn">
                  No cap — a long session keeps every event, which the panel
                  eventually feels.
                </Note>
              )}
            </Block>
          ),
        },
      ],
    },
    {
      // Ticked in the rail, applied to the stream beside it — no section to
      // leave and come back from (operator, 2026-08-14).
      label: 'Tags',
      filters: ALL_PULSE_TAGS.map((tag) => ({
        key: tag,
        label: tag,
        count: tagCounts.get(tag) ?? 0,
        checked: isTagEnabled(tag),
        hint: TAG_HINTS[tag],
      })),
      onToggleFilter: (key) => toggleTag(key as PulseTag),
      onResetFilters: resetFilter,
    },
  ];

  return <RailView groups={groups} label="Pulse sections" />;
}
