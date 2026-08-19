import { useEffect, useState } from "react";
import { useShallow } from "zustand/react/shallow";
import {
  isRunningStatus,
  useActivityStore,
  type ActivityRecord,
} from "../../stores/activity";
import {
  openActivity,
  OPENABLE_ACTIVITY_KINDS,
} from "../../canvas/openActivity";
import { formatRelative } from "../../lib/time";
import { ActivityDetail } from "../activity/ActivityDetail";
import { Hint } from '../ui/Hint';
import { MenuItem } from '../common/MenuItem';
import { Chip } from '../common/Chip';

/**
 * Task 6.2 — always-visible taskbar for ALL running workstream kinds
 * (delegate incl. invoke_agent, lane, controller_session, routine,
 * autonomy, mcp_session). Replaces `RunningSpawnsChip`, which only read
 * `cliStreams` (CLI delegate subprocesses) plus a lane/controller_session
 * subset of the activity registry — invoke_agent spawns register in the
 * activity registry under the same `delegate` kind as delegate_coder/auditor
 * but never got a cliStream entry (no subprocess), so they never appeared.
 *
 * Reading `useActivityStore.records()` directly avoids that gap and avoids
 * double-counting: `delegate` kind already covers every background spawn
 * (delegate_coder/auditor, invoke_agent, delegate_agent_controller, lane_turn),
 * so there's no second cliStreams-based list to reconcile against it.
 */
export function ActivityTaskbar() {
  // useShallow: `records()` rebuilds a sorted array each call; without
  // value-equality the new reference would retrigger useSyncExternalStore
  // every render (same infinite-loop hazard documented on ActivityMap).
  const records = useActivityStore(useShallow((s) => s.records()));
  const [expanded, setExpanded] = useState(false);
  const [openDetailId, setOpenDetailId] = useState<string | null>(null);
  // Live-tick for the "age" display so rows update without a store mutation.
  const [, setTick] = useState(0);

  const running = records.filter((r) => isRunningStatus(r.state));

  useEffect(() => {
    if (running.length === 0) return;
    const id = window.setInterval(() => setTick((t) => t + 1), 1000);
    return () => window.clearInterval(id);
  }, [running.length]);

  if (running.length === 0) return null;

  function onRowClick(r: ActivityRecord) {
    if (OPENABLE_ACTIVITY_KINDS.has(r.kind)) {
      void openActivity(r);
      return;
    }
    setOpenDetailId((id) => (id === r.activity_id ? null : r.activity_id));
  }

  return (
    <div
      className="activity-taskbar-strip"
      role="region"
      aria-label="Active workstreams"
    >
      <Chip
        onClick={() => setExpanded((v) => !v)}
        active={expanded}
        ariaExpanded={expanded}
      >
        <span className="activity-taskbar-glyph">↻</span>
        <span className="activity-taskbar-count">{running.length} running</span>
      </Chip>
      {expanded && (
        <div className="activity-taskbar-list">
          {running.map((r) => {
            const openable = OPENABLE_ACTIVITY_KINDS.has(r.kind);
            return (
              <div key={r.activity_id} className="activity-taskbar-row-wrap">
                <Hint label={openable
                      ? "Open this work on the cockpit canvas"
                      : "Show details"}>
                  <MenuItem
                    className="activity-taskbar-row"
                    onClick={() => onRowClick(r)}
                    ariaLabel={`${openable ? "Open" : "Show details for"} ${r.label}`}
                  >
                    <span
                      className={`activity-dot activity-dot--${r.state}`}
                      aria-hidden="true"
                    />
                    <span className="activity-taskbar-label">{r.label}</span>
                    <span className="activity-taskbar-age t-meta">
                      {formatRelative(r.started_at)}
                    </span>
                  </MenuItem>
                </Hint>
                {!openable && openDetailId === r.activity_id && (
                  <ActivityDetail record={r} />
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
