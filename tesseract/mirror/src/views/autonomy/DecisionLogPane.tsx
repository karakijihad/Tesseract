// What the kernel and the governor decided, most recent first.
//
// Built from two signals the panel already holds: the last transition on each
// agenda item, and the governor's last tick. One band of state lines.
//
// It drew bordered cards with two chips in the head of each before, which is
// the shape the panel replaced everywhere else.

import React from 'react';
import type { AgendaItem, GovernorTickPayload } from '../../lib/api';
import { Band, StateStrip, type StateLine } from '../../components/common/StateStrip';
import { clock } from '../../lib/time';

const SHOWN = 10;

// A transition into one of these did not go the way it was meant to. The rest
// are the kernel doing what it said it would.
const BADLY = new Set(['failed', 'cancelled', 'abandoned', 'blocked']);

interface Decision {
  key: string;
  at: string;
  by: string;
  became: string;
  detail: string;
}

function decisions(
  items: AgendaItem[],
  lastTick: GovernorTickPayload | null,
): Decision[] {
  const out: Decision[] = [];
  for (const item of items) {
    const last = item.status_history[item.status_history.length - 1];
    if (!last) continue;
    out.push({
      key: `${item.id}:${last.at}`,
      at: last.at,
      by: last.by,
      became: last.to_status,
      detail: last.reason || item.goal,
    });
  }
  if (
    lastTick &&
    (lastTick.pauses_added.length ||
      lastTick.workers_cancelled.length ||
      lastTick.items_blocked.length)
  ) {
    out.push({
      key: `governor:${lastTick.at}`,
      at: lastTick.at,
      by: 'governor',
      became: 'stepped in',
      detail: [
        lastTick.pauses_added.length ? `paused ${lastTick.pauses_added.length}` : '',
        lastTick.workers_cancelled.length
          ? `cancelled ${lastTick.workers_cancelled.length}`
          : '',
        lastTick.items_blocked.length ? `blocked ${lastTick.items_blocked.length}` : '',
      ]
        .filter(Boolean)
        .join(', '),
    });
  }
  return out.sort((a, b) => (a.at < b.at ? 1 : -1)).slice(0, SHOWN);
}

function toLine(row: Decision): StateLine {
  return {
    key: row.key,
    state: BADLY.has(row.became) ? 'degraded' : 'idle',
    label: row.became.replace(/_/g, ' '),
    name: row.by,
    said: row.detail,
    when: clock(row.at),
    value: row.became.replace(/_/g, ' '),
  };
}

export function DecisionLogPane({
  items,
  lastTick,
}: {
  items: AgendaItem[];
  lastTick: GovernorTickPayload | null;
}): React.ReactElement {
  const rows = decisions(items, lastTick);
  if (rows.length === 0) {
    return <p className="t-meta">Nothing has been decided yet.</p>;
  }
  return (
    <div className="autonomy-group">
      <Band label="What it decided" count={rows.length} />
      <StateStrip lines={rows.map(toLine)} />
    </div>
  );
}
