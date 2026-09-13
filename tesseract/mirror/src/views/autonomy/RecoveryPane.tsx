// The last recovery pass, and what it reconciled.
//
// What the runtime found half-finished when it came back up: conversations cut
// mid answer, scheduled runs that never reported, agenda items left in flight.
// One band of state lines, like every room on this panel, and the things that
// want the operator are their own band above the counts.
//
// Rendered in two places, which is why it takes its summary as a prop rather
// than fetching: the Autonomy panel's Recent outcomes room, and the Settings
// Runtime section.

import React from 'react';
import { Band, StateStrip, type StateLine } from '../../components/common/StateStrip';
import { Note } from '../../components/common/Note';
import { formatRelative } from '../../lib/time';
import type { RecoveryAttention } from '../../lib/api';
import { useAutonomyStore, type AutonomyLevel } from '../../stores/autonomy';


export interface RecoveryScans {
  [scan: string]: { [bucket: string]: number };
}

export interface RecoverySummaryPayload {
  boot_id: string | null;
  downtime_seconds: number;
  scans: RecoveryScans;
  operator_attention: RecoveryAttention[];
  started_at: string | null;
}

export interface RecoveryPaneProps {
  summary: RecoverySummaryPayload | null;
  recoveryState: 'recovering' | 'ready';
}

// What each scan looked at, in the operator's words rather than the scanner's
// own name. A scan with no words is shown under its own name rather than
// hidden, so a new one appears here without anybody editing this.
const SCAN_LABEL: Record<string, string> = {
  turns: 'conversations',
  schedule: 'scheduled runs',
  workers: 'workers',
  agenda: 'agenda items',
};

// What one of these things left half-finished actually is, in the panel's own
// vocabulary. An agenda item and a worker open into their own record. An
// effect opens into the same clarification card recovery already filed about
// it: the same question, the same thread, answered from here rather than a
// second version of it. A conversation opens into the same turn record the
// Conscience panel reads, in place, the same way: `TurnRecord` is the one
// renderer for what a turn did, and this room reuses it rather than sending
// the operator across to read a second version of it.
const ATTENTION_OPENS: Partial<Record<string, AutonomyLevel['kind']>> = {
  agenda: 'agenda',
  worker: 'worker',
  effect: 'effect',
  turn: 'turn',
};

/** What one attention item opens as, or `undefined` when it has nowhere to
 *  go. Everything but `turn` is a straight lookup. A `turn` needs its `day`
 *  too, because opening it means reading one specific day's record back, and
 *  a turn whose manifest could not be read at boot never got one: recovery
 *  said so already, in the reason on the row, and there is nothing behind it
 *  to open. */
function opensAs(a: RecoveryAttention): AutonomyLevel['kind'] | undefined {
  if (a.kind === 'turn' && !a.day) return undefined;
  return ATTENTION_OPENS[a.kind];
}

// The plain statement for a row `opensAs` has nothing for: a scan failing,
// which has nothing beyond the error message already on it, and a turn whose
// record could not be read at all, which already says so on the same line.
// One sentence covers both, because neither has anything further to add once
// a turn WITH a day opens in place like everything else on this band.
const CANNOT_OPEN =
  'This cannot be opened from here. The reason above is everything ' +
  'currently known about it.';

/** What one scan found, as a sentence. Empty when it found nothing, so a scan
 *  with nothing to report is not drawn at all. */
function found(buckets: { [bucket: string]: number }): string {
  return Object.entries(buckets)
    .filter(([, count]) => count > 0)
    .map(([bucket, count]) => `${count} ${bucket.replace(/_/g, ' ')}`)
    .join(', ');
}

export function RecoveryPane({
  summary,
  recoveryState,
}: RecoveryPaneProps): React.ReactElement {
  const pushLevel = useAutonomyStore((s) => s.pushLevel);
  if (recoveryState === 'recovering') {
    return (
      <Note tone="warn">
        The runtime is still reconciling what it found at boot. Its heartbeat is
        suspended until that finishes, so anything asking whether it is healthy
        is told no.
      </Note>
    );
  }
  if (!summary || !summary.boot_id) {
    return (
      <p className="t-meta">
        This backend has not recovered anything since it started.
      </p>
    );
  }

  const attn = summary.operator_attention;
  const scans: StateLine[] = Object.entries(summary.scans)
    .map(([scan, buckets]) => ({ scan, said: found(buckets) }))
    .filter((s) => s.said)
    .map(({ scan, said }) => ({
      key: `scan:${scan}`,
      // Reconciling is the runtime working, not a fault. What wants the
      // operator is in the band above, and it says so itself.
      state: 'idle' as const,
      label: 'reconciled',
      name: SCAN_LABEL[scan] ?? scan.replace(/_/g, ' '),
      said,
    }));

  return (
    <>
      <p className="t-meta">
        What the runtime found half-finished when it last started, and whether any of it is still waiting on you.
      </p>
      {attn.length > 0 && (
        <div className="autonomy-group">
          <Band label="Left half-finished for you" count={attn.length} />
          <StateStrip
            lines={attn.map((a, i) => {
              const opens = opensAs(a);
              return {
                key: `${a.kind}:${a.id}:${i}`,
                state: 'pending' as const,
                label: 'waiting on you',
                name: a.kind.replace(/_/g, ' '),
                said: a.reason,
                value: a.id,
                onOpen: opens
                  ? () =>
                      pushLevel({
                        kind: opens,
                        id: a.id,
                        label: a.reason || a.id,
                        day: a.day,
                      })
                  : undefined,
                more: opens ? undefined : <p className="t-meta">{CANNOT_OPEN}</p>,
              };
            })}
          />
        </div>
      )}

      <div className="autonomy-group">
        <Band label="What the last boot reconciled" count={scans.length} />
        {scans.length === 0 ? (
          <p className="t-meta">
            A clean boot. Nothing was left half-finished, {formatRelative(summary.started_at)}.
          </p>
        ) : (
          <StateStrip lines={scans} />
        )}
      </div>
    </>
  );
}
