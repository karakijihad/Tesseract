import { useCallback, useMemo, useState } from 'react';

import { Button } from '../components/common/Button';
import { RailView, type RailGroup } from '../components/common/RailView';
import { useWorkspaceStore, type EventKind } from '../stores/workspace';
import {
  InboxPanel,
  INBOX_FILTERS,
  KIND_LABEL,
  type InboxFilter,
} from './workspace/InboxPanel';
import { DailyBriefTab } from './workspace/DailyBriefTab';
import './workspace/WorkspaceView.css';

/** Inbox slices are rail sections and event kinds are rail filters — the two
 *  stacked chip rows they replace sat above the list, pushing it down the
 *  screen, and said nothing about which one narrowed which (operator,
 *  2026-08-14). */
export function WorkspaceView() {
  const [filter, setFilter] = useState<InboxFilter>('all');
  const [kinds, setKinds] = useState<Set<EventKind> | null>(null);
  const [kindCounts, setKindCounts] = useState<Map<EventKind, number>>(new Map());

  const loading = useWorkspaceStore((s) => s.loading);
  const loadingHistory = useWorkspaceStore((s) => s.loadingHistory);
  const fetchInbox = useWorkspaceStore((s) => s.fetchInbox);
  const fetchHistory = useWorkspaceStore((s) => s.fetchHistory);
  const fetchSeen = useWorkspaceStore((s) => s.fetchSeen);
  const events = useWorkspaceStore((s) => s.events);
  const attentionCount = useWorkspaceStore((s) => s.attentionCount);

  // Identity is what makes this stable across renders; without it the effect
  // reporting kinds back up would fire on every one.
  const onKindsAvailable = useCallback((counts: Map<EventKind, number>) => {
    setKindCounts((prev) => {
      if (prev.size === counts.size &&
          [...counts].every(([k, v]) => prev.get(k) === v)) {
        return prev;
      }
      return counts;
    });
  }, []);

  const refresh = () => {
    if (filter === 'history') fetchHistory();
    else fetchInbox();
    fetchSeen();
  };

  const inboxActions = (
    <Button
      onClick={refresh}
      disabled={loading || loadingHistory}
      ariaLabel="Refresh inbox"
    >
      {loading || loadingHistory ? '…' : 'refresh'}
    </Button>
  );

  const attention = attentionCount();
  const meta = `${events.length} pending${attention > 0 ? ` · ${attention} waiting >24h` : ''}`;

  const groups: RailGroup[] = useMemo(() => {
    const kindRows = [...kindCounts.entries()].sort((a, b) => b[1] - a[1]);
    return [
      {
        label: 'Inbox',
        sections: INBOX_FILTERS.map((f) => ({
          key: f.key,
          label: f.label,
          meta,
          actions: inboxActions,
          render: () => (
            <InboxPanel
              filter={f.key}
              kinds={kinds}
              onKindsAvailable={onKindsAvailable}
            />
          ),
        })),
      },
      ...(kindRows.length > 1
        ? [
            {
              label: 'Kinds',
              filters: kindRows.map(([kind, count]) => ({
                key: kind,
                label: KIND_LABEL[kind] ?? kind,
                count,
                checked: kinds === null || kinds.has(kind),
              })),
              onToggleFilter: (key: string) => {
                const kind = key as EventKind;
                setKinds((prev) => {
                  const all = new Set(kindRows.map(([k]) => k));
                  const next = new Set(prev ?? all);
                  if (next.has(kind)) next.delete(kind);
                  else next.add(kind);
                  // Everything ticked is the same as no filter, and saying so
                  // keeps the "all" state out of the counts.
                  return next.size === all.size ? null : next;
                });
              },
              onResetFilters: () => setKinds(null),
            },
          ]
        : []),
      {
        label: 'Digest',
        sections: [{ key: 'brief', label: 'Daily Brief', Body: DailyBriefTab }],
      },
    ];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kindCounts, kinds, meta, loading, loadingHistory, filter]);

  return (
    <RailView
      groups={groups}
      label="Workspace sections"
      initial={filter}
      onSectionChange={(key) => {
        if (key !== 'brief') setFilter(key as InboxFilter);
      }}
    />
  );
}
