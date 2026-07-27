// AU-7 Phase 3 — PrunedPane.
//
// Surfaces what the autonomy admission gate discarded, bucketed by
// source x stage, so a recurrent-useless source is visible at a glance
// and mutable (mute the source's future proposals) in one click.
//
// Split into a pure `PrunedPaneView` (props in, JSX out — matches the
// prop-driven pattern the other panes use, e.g. JournalPane) and a
// connected `PrunedPane` wrapper that self-loads from the store. The
// pruned ledger isn't part of the fetchAll() dashboard fan-out, so the
// wrapper owns its own mount-time fetch (like CodeDriftChip /
// NotificationsPane).

import { useEffect } from 'react';
import type { PrunedResponse } from '../../lib/api';
import { formatRelative } from '../../lib/time';
import { useAutonomyStore } from '../../stores/autonomy';

const STAGES = ['malformed', 'duplicate', 'low_value', 'capped'] as const;

// Default lookback for the counts table — matches the route's own
// default (`GET /api/autonomy/pruned?window_hours=168`).
const DEFAULT_WINDOW_HOURS = 168;

// A source at/above this many prunes in the window is flagged as the
// recurrent-useless signal.
const HOT_THRESHOLD = 10;

const RECENT_CAP = 20;
const GOAL_TRUNCATE = 60;

function _truncateGoal(goal: string): string {
  return goal.length > GOAL_TRUNCATE ? `${goal.slice(0, GOAL_TRUNCATE)}…` : goal;
}

function _sourceTotal(stageCounts: Record<string, number>): number {
  return Object.values(stageCounts).reduce((sum, n) => sum + n, 0);
}

export interface PrunedPaneViewProps {
  pruned: PrunedResponse | null;
  prunedStatus: 'idle' | 'loading' | 'error';
  mutedSources: Set<string>;
  pending: Set<string>;
  onMute: (source: string, muted: boolean) => void;
  onRefresh: () => void;
}

export function PrunedPaneView({
  pruned,
  prunedStatus,
  mutedSources,
  pending,
  onMute,
  onRefresh,
}: PrunedPaneViewProps): React.ReactElement {
  if (prunedStatus === 'error') {
    return (
      <section className="runtime-block autonomy-pane autonomy-pane--pruned">
        <div className="runtime-block__title">Pruned</div>
        <p className="t-meta">Failed to load pruned ledger.</p>
      </section>
    );
  }

  if (pruned === null) {
    return (
      <section className="runtime-block autonomy-pane autonomy-pane--pruned">
        <div className="runtime-block__title">Pruned</div>
        <p className="t-meta">Loading…</p>
      </section>
    );
  }

  const sources = Object.keys(pruned.counts).sort(
    (a, b) => _sourceTotal(pruned.counts[b]) - _sourceTotal(pruned.counts[a]),
  );
  const recent = pruned.records.slice(0, RECENT_CAP);

  return (
    <section className="runtime-block autonomy-pane autonomy-pane--pruned" data-testid="autonomy-pruned-pane">
      <div className="runtime-block__title">
        Pruned
        <span className="t-meta" style={{ marginLeft: 8 }}>
          {DEFAULT_WINDOW_HOURS}h window · {pruned.records.length} records
        </span>
        <button
          type="button"
          className="autonomy-view__refresh"
          style={{ marginLeft: 8 }}
          onClick={onRefresh}
          aria-label="refresh pruned ledger"
        >
          refresh
        </button>
      </div>

      {sources.length === 0 ? (
        <p className="t-meta">Nothing pruned in this window.</p>
      ) : (
        <table className="pruned-table" data-testid="pruned-counts-table">
          <thead>
            <tr>
              <th className="t-meta">source</th>
              {STAGES.map((stage) => (
                <th key={stage} className="t-meta">{stage}</th>
              ))}
              <th className="t-meta">mute</th>
            </tr>
          </thead>
          <tbody>
            {sources.map((source) => {
              const stageCounts = pruned.counts[source];
              const total = _sourceTotal(stageCounts);
              const hot = total >= HOT_THRESHOLD;
              const muted = mutedSources.has(source);
              const busy = pending.has(`prune-mute:${source}`);
              return (
                <tr key={source} className={hot ? 'pruned-table__row--hot' : undefined}>
                  <td>{source}</td>
                  {STAGES.map((stage) => (
                    <td key={stage} className="t-meta">
                      {stageCounts[stage] ?? 0}
                    </td>
                  ))}
                  <td>
                    <button
                      type="button"
                      className="autonomy-btn"
                      onClick={() => onMute(source, !muted)}
                      disabled={busy}
                    >
                      {muted ? 'Muted' : 'Mute'}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      <div className="autonomy-pane__group-title">recent prunes</div>
      {recent.length === 0 ? (
        <p className="t-meta">No prune records.</p>
      ) : (
        <ul className="autonomy-list" data-testid="pruned-recent-list">
          {recent.map((rec, idx) => (
            <li
              key={`${rec.ts}-${rec.source}-${idx}`}
              className="autonomy-row autonomy-row--pruned"
            >
              <div className="autonomy-row__head">
                <span className="autonomy-chip autonomy-chip--source">{rec.source}</span>
                <span className="autonomy-chip">{rec.stage}</span>
                <span className="t-meta">{formatRelative(rec.ts)}</span>
              </div>
              <div className="autonomy-row__goal t-meta">{_truncateGoal(rec.goal)}</div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function PrunedPane(): React.ReactElement {
  const pruned = useAutonomyStore((s) => s.pruned);
  const prunedStatus = useAutonomyStore((s) => s.prunedStatus);
  const loadPruned = useAutonomyStore((s) => s.loadPruned);
  const muteSource = useAutonomyStore((s) => s.muteSource);
  // Select the stable `governor.data` ref (null until loaded, then a
  // steady object) — NOT `?.pauses ?? []`, whose fresh `[]` on every call
  // made useSyncExternalStore's snapshot change each render and spun the
  // "getSnapshot should be cached" infinite loop that blanked the view.
  // The `?? []` fallback lives in the render body below, where a fresh
  // array is harmless.
  const governorData = useAutonomyStore((s) => s.governor.data);
  const pending = useAutonomyStore((s) => s.pendingActions);

  useEffect(() => {
    void loadPruned(DEFAULT_WINDOW_HOURS);
  }, [loadPruned]);

  const mutedSources = new Set((governorData?.pauses ?? []).map((p) => p.source));

  return (
    <PrunedPaneView
      pruned={pruned}
      prunedStatus={prunedStatus}
      mutedSources={mutedSources}
      pending={pending}
      onMute={(source, muted) => void muteSource(source, muted)}
      onRefresh={() => void loadPruned(DEFAULT_WINDOW_HOURS)}
    />
  );
}
