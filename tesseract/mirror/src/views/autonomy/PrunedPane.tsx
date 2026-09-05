// What was dropped at the door, and why.
//
// The admission gate turns drafts away before they become work. Two bands: the
// sources it turned away, worst first, each with the mute that stops a
// recurrently useless one; and the most recent drafts themselves.
//
// It drew a source-by-stage table and a second list of bordered cards before.
// The table was the only one on this panel and it read as a spreadsheet in a
// room of state lines, so a source is a row now and its stages are the
// sentence beside it.

import { useEffect } from 'react';
import { Button } from '../../components/common/Button';
import { Note } from '../../components/common/Note';
import { RowActions } from '../../components/common/Row';
import type { PrunedResponse } from '../../lib/api';
import { useAutonomyStore } from '../../stores/autonomy';
import { Band, StateStrip, type StateLine } from '../../components/common/StateStrip';
import { clock } from '../../lib/time';

// The route's own default (`GET /api/autonomy/pruned?window_hours=168`).
const DEFAULT_WINDOW_HOURS = 168;

// A source at or above this many in the window is the recurrently-useless
// signal the mute exists for.
const HOT_THRESHOLD = 10;

const RECENT_CAP = 12;

// What each stage of the gate is called. The slugs are the code's; a person
// reading a room needs what the gate decided.
const STAGE_LABEL: Record<string, string> = {
  malformed: 'malformed',
  duplicate: 'already had it',
  low_value: 'not worth doing',
  capped: 'over the daily cap',
};

function total(stages: Record<string, number>): number {
  return Object.values(stages).reduce((sum, n) => sum + n, 0);
}

/** What the gate did to this source, in its own terms. */
function why(stages: Record<string, number>): string {
  return Object.entries(stages)
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([stage, n]) => `${n} ${STAGE_LABEL[stage] ?? stage}`)
    .join(', ');
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
    return <Note tone="bad">What was dropped at the door could not be read.</Note>;
  }
  if (pruned === null) return <></>;

  const sources = Object.keys(pruned.counts).sort(
    (a, b) => total(pruned.counts[b]) - total(pruned.counts[a]),
  );

  const sourceLines: StateLine[] = sources.map((source) => {
    const count = total(pruned.counts[source]);
    const muted = mutedSources.has(source);
    const busy = pending.has(`prune-mute:${source}`);
    return {
      key: `source:${source}`,
      // Turning a draft away is the gate working, so a source is quiet however
      // many it dropped. What wants the operator is one that keeps coming
      // back, and that is what the threshold marks.
      state: muted ? 'idle' : count >= HOT_THRESHOLD ? 'degraded' : 'idle',
      label: muted ? 'muted' : count >= HOT_THRESHOLD ? 'keeps coming back' : 'quiet',
      name: source,
      said: why(pruned.counts[source]),
      value: String(count),
      actions: (
        <RowActions className="state-acts">
          <Button
            onClick={() => onMute(source, !muted)}
            disabled={busy}
            ariaLabel={`${muted ? 'Unmute' : 'Mute'} ${source}`}
          >
            {muted ? 'unmute' : 'mute'}
          </Button>
        </RowActions>
      ),
    };
  });

  const recent = pruned.records.slice(0, RECENT_CAP);

  return (
    <div data-testid="autonomy-pruned-pane">
      <div className="managed-head">
        <span className="t-meta">
          The last {DEFAULT_WINDOW_HOURS} hours, {pruned.records.length} in all
        </span>
        <Button onClick={onRefresh} ariaLabel="read the pruned ledger again">
          refresh
        </Button>
      </div>

      {sources.length === 0 ? (
        <p className="t-meta">Nothing was turned away in this window.</p>
      ) : (
        <div className="autonomy-group">
          <Band label="Where they came from" count={sources.length} />
          <StateStrip lines={sourceLines} />
        </div>
      )}

      {recent.length > 0 && (
        <div className="autonomy-group">
          <Band label="The most recent" count={pruned.records.length} />
          <StateStrip
            lines={recent.map((rec, i) => ({
              key: `${rec.ts}:${rec.source}:${i}`,
              state: 'idle' as const,
              label: STAGE_LABEL[rec.stage] ?? rec.stage,
              name: rec.source,
              said: rec.goal,
              when: clock(rec.ts),
              value: STAGE_LABEL[rec.stage] ?? rec.stage,
            }))}
          />
        </div>
      )}
    </div>
  );
}

export function PrunedPane(): React.ReactElement {
  const pruned = useAutonomyStore((s) => s.pruned);
  const prunedStatus = useAutonomyStore((s) => s.prunedStatus);
  const loadPruned = useAutonomyStore((s) => s.loadPruned);
  const muteSource = useAutonomyStore((s) => s.muteSource);
  // The stable `governor.data` ref, NOT `?.pauses ?? []`, whose fresh `[]` on
  // every call made useSyncExternalStore's snapshot change each render and
  // spun the "getSnapshot should be cached" loop that blanked the view.
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
