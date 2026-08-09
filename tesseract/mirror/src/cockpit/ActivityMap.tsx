// AS-2 — the live map: a floating glass card listing ALL running work the
// AS-1 registry knows about, grouped by substrate. Client-only reflection —
// no persistence. A row opens that unit's own surface (openActivity) when one
// exists; kinds with no surface (routine | autonomy | mcp_session) expand an
// inline detail block instead (HUD runs-surface fix).

import { useState } from "react";

import { useShallow } from "zustand/react/shallow";
import {
  isRunningStatus,
  useActivityStore,
  type ActivityRecord,
} from "../stores/activity";
import { openActivity, OPENABLE_ACTIVITY_KINDS } from "../canvas/openActivity";
import { closeActivity, closeAllActivities } from "../lib/api";
import { nextZ, recordSurfaceZ } from "./zStack";
import { useDraggable } from "./useDraggable";
import { ActivityDetail } from "../components/activity/ActivityDetail";

const GROUPS: Array<{ kind: string; title: string }> = [
  { kind: "lane", title: "Lanes" },
  { kind: "mcp_session", title: "MCP sessions" },
  { kind: "delegate", title: "Delegates" },
  { kind: "controller_session", title: "Sessions" },
  { kind: "routine", title: "Routines" },
  { kind: "autonomy", title: "Autonomy" },
];

// Kinds close-all cancels (mirrors the server allowlist). Sessions/routines/
// autonomy have their own lifecycle and are left alone.
const CLOSEABLE = new Set(["lane", "mcp_session", "delegate"]);

// N3 — running-state classification lives in one place (stores/activity.ts's
// isRunningStatus); importing it prevents a drifted duplicate here.

// Non-running rows per group beyond this cap collapse behind "+N older".
const OLDER_CAP = 5;

interface ActivityMapProps {
  onClose: () => void;
}

export function ActivityMap({ onClose }: ActivityMapProps) {
  // useShallow: `records()` rebuilds a sorted array each call; without
  // value-equality the new reference would retrigger useSyncExternalStore every
  // render → "getSnapshot should be cached" infinite loop (crashed the cockpit
  // once the registry held many records). Shallow-comparing the element refs
  // keeps the snapshot stable until a record actually changes.
  const records = useActivityStore(useShallow((s) => s.records()));
  // Allocate a shared-stack z once on open; bump it on interaction so the map
  // participates in the same last-focused-wins order as panels + surfaces.
  // (Lazy initializer keeps nextZ() out of the render body.)
  const [z, setZ] = useState(() => {
    const _z = nextZ();
    recordSurfaceZ(_z);
    return _z;
  });
  // v2: the map's default anchor moved from bottom (near the retired
  // ActivityPill) to below the permanent top HUD segment (change-set B) —
  // versioned so a stale bottom-anchored position from before doesn't pin it.
  const { ref, style, onDragStart } = useDraggable(
    "tesseract.cockpit.activityMap.pos.v2",
  );
  const [closing, setClosing] = useState(false);
  const [showOlder, setShowOlder] = useState<Record<string, boolean>>({});

  const closeableCount = records.filter((r) => CLOSEABLE.has(r.kind)).length;

  async function onCloseAll() {
    if (closing || closeableCount === 0) return;
    if (
      !window.confirm(
        `Close all ${closeableCount} running unit(s)? This terminates open lanes and MCP sessions.`,
      )
    )
      return;
    setClosing(true);
    try {
      await closeAllActivities(); // chips clear via the activity WS push
    } catch {
      // swallow — the store reflects whatever actually closed
    } finally {
      setClosing(false);
    }
  }

  return (
    <div
      ref={ref as React.RefObject<HTMLDivElement>}
      className="activity-map"
      style={{ ...style, zIndex: z }}
      onPointerDown={() =>
        setZ(() => {
          const _z = nextZ();
          recordSurfaceZ(_z);
          return _z;
        })
      }
    >
      <div className="activity-map__bar" onPointerDown={onDragStart}>
        <span className="activity-map__title">Routing</span>
        <div className="activity-map__actions">
          <button
            type="button"
            className="activity-map__closeall"
            disabled={closing || closeableCount === 0}
            aria-label="Close all running units"
            title="Cancel all lanes, MCP sessions and delegates"
            onClick={onCloseAll}
            onPointerDown={(e) => e.stopPropagation()}
          >
            {closing
              ? "Closing…"
              : `Close all${closeableCount ? ` (${closeableCount})` : ""}`}
          </button>
          <button
            type="button"
            className="activity-map__close"
            aria-label="Close map"
            onClick={onClose}
            onPointerDown={(e) => e.stopPropagation()}
          >
            ×
          </button>
        </div>
      </div>
      <div className="activity-map__body">
        {GROUPS.map(({ kind, title }) => {
          const rows = records.filter((r) => r.kind === kind);
          if (rows.length === 0) return null;
          // `records` is already running-first / newest-first (stores/activity.ts
          // records()), so filtering by kind preserves that order — running rows
          // always render, non-running rows past the cap collapse.
          const runningRows = rows.filter((r) => isRunningStatus(r.state));
          const olderRows = rows.filter((r) => !isRunningStatus(r.state));
          const expanded = showOlder[kind] ?? false;
          const visibleOlder = expanded
            ? olderRows
            : olderRows.slice(0, OLDER_CAP);
          const hiddenCount = olderRows.length - visibleOlder.length;
          return (
            <div key={kind} className="activity-map__group">
              <div className="activity-map__group-title t-meta">{title}</div>
              {[...runningRows, ...visibleOlder].map((r) => (
                <ActivityRow key={r.activity_id} record={r} />
              ))}
              {olderRows.length > OLDER_CAP && (
                <button
                  type="button"
                  className="activity-map__more t-meta"
                  onClick={() =>
                    setShowOlder((s) => ({ ...s, [kind]: !expanded }))
                  }
                >
                  {expanded ? "show less" : `+${hiddenCount} older`}
                </button>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function ActivityRow({ record }: { record: ActivityRecord }) {
  const failed = record.state === "failed";
  const detailable = !OPENABLE_ACTIVITY_KINDS.has(record.kind);
  // A failed chip stays until dismissed (operator must not miss failures) —
  // its close button always shows, regardless of kind, and dismissal means
  // remove-from-registry (see close_one/close_all's failed-state branch).
  const closeable = CLOSEABLE.has(record.kind) || failed;
  const [expanded, setExpanded] = useState(false);
  const [closing, setClosing] = useState(false);

  async function onCloseOne(e: React.MouseEvent) {
    e.stopPropagation();
    if (closing) return;
    setClosing(true);
    try {
      await closeActivity(record.activity_id); // chip clears via the activity WS push
    } catch {
      setClosing(false);
    }
  }

  return (
    <div className="activity-map__row-wrap">
      <div
        role="button"
        tabIndex={0}
        className={`activity-map__row${failed ? " activity-map__row--failed" : ""}`}
        onClick={() =>
          detailable ? setExpanded((v) => !v) : void openActivity(record)
        }
        onKeyDown={(e) => {
          if (e.target !== e.currentTarget) return; // let the nested close button handle its own key
          if (e.key !== "Enter" && e.key !== " ") return;
          e.preventDefault();
          detailable ? setExpanded((v) => !v) : void openActivity(record);
        }}
        title={detailable ? "Show details" : "Open this work on the cockpit"}
        aria-expanded={detailable ? expanded : undefined}
      >
        <span
          className={`activity-dot activity-dot--${record.state}`}
          aria-hidden="true"
        />
        <span className="activity-map__label">{record.label}</span>
        {record.provider && (
          <span className="activity-map__provider t-meta">
            {record.provider}
          </span>
        )}
        <span className="activity-map__state t-meta">{record.state}</span>
        {closeable && (
          <button
            type="button"
            className="activity-map__row-close"
            aria-label={`Close ${record.label}`}
            title="Close this unit"
            disabled={closing}
            onClick={onCloseOne}
          >
            {closing ? "…" : "×"}
          </button>
        )}
        {detailable && (
          <span
            className={`activity-map__chevron t-meta${expanded ? " is-open" : ""}`}
            aria-hidden="true"
          >
            ›
          </span>
        )}
      </div>
      {detailable && expanded && <ActivityDetail record={record} />}
    </div>
  );
}
