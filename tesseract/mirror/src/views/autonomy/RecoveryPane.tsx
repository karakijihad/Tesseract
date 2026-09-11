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
import { useAutonomyStore, type AutonomyLevel } from '../../stores/autonomy';

export interface RecoveryScans {
  [scan: string]: { [bucket: string]: number };
}

export interface RecoveryAttention {
  kind: string;
  id: string;
  reason: string;
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
// vocabulary. Only an agenda item and a worker have somewhere to open; a
// conversation, an effect or a scan itself do not, so those rows keep their
// reason on screen and stay plain rather than pretending there is a place to
// go.
const ATTENTION_OPENS: Record<string, AutonomyLevel['kind']> = {
  agenda: 'agenda',
  worker: 'worker',
};

// The plain statement for a kind `ATTENTION_OPENS` has nothing for. The row
// stays a plain line rather than a `Row` (no click, no role=button), and this
// is the sentence that says why in words, so a reader is told rather than
// left to guess from the absence of a cursor.
const CANNOT_OPEN_SAYS =
  'This cannot be opened from here. The reason above is everything currently known about it.';

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
              const opens = ATTENTION_OPENS[a.kind];
              return {
                key: `${a.kind}:${a.id}:${i}`,
                state: 'pending' as const,
                label: 'waiting on you',
                name: a.kind.replace(/_/g, ' '),
                said: a.reason,
                value: a.id,
                onOpen: opens
                  ? () => pushLevel({ kind: opens, id: a.id, label: a.reason || a.id })
                  : undefined,
                more: opens ? undefined : <p className="t-meta">{CANNOT_OPEN_SAYS}</p>,
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
