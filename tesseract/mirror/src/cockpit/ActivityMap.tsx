// AS-2 — the live map: a floating glass card listing ALL running work the
// AS-1 registry knows about, grouped by substrate. Client-only reflection —
// no persistence. A row opens that unit's own surface (openActivity) when one
// exists; kinds with no surface (routine | autonomy | mcp_session) expand an
// inline detail block instead (HUD runs-surface fix).

import { Button } from "../components/common/Button";
import { CloseButton } from "../components/common/CloseButton";
import { Disclosure } from "../components/common/Disclosure";
import { IconButton } from "../components/common/IconButton";
import { ResetIcon } from "../components/common/icons";
import { ResizeHandles } from "../components/common/ResizeHandles";
import { Row, RowActions } from "../components/common/Row";
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
import { Hint } from '../components/ui/Hint';

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
  const { ref, style, onDragStart, onResizeStart, reset, moved } = useDraggable(
    "tesseract.cockpit.activityMap.pos.v2",
  );
  const [closing, setClosing] = useState(false);
  const [showOlder, setShowOlder] = useState<Record<string, boolean>>({});

  const closeableCount = records.filter((r) => CLOSEABLE.has(r.kind)).length;

  // The bar drags the card, so a control inside it has to stop the pointer
  // before the drag begins.
  const stopDrag = (e: React.PointerEvent) => e.stopPropagation();

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
          <Hint label="Cancel all lanes, MCP sessions and delegates">
            <Button
              disabled={closing || closeableCount === 0}
              ariaLabel="Close all running units"
              onClick={onCloseAll}
            >
              {closing
                ? "Closing…"
                : `Close all${closeableCount ? ` (${closeableCount})` : ""}`}
            </Button>
          </Hint>
          <Hint label="Reset Routing to its default position and size">
            <IconButton
              ariaLabel="Reset Routing to its default position and size"
              disabled={!moved}
              onClick={reset}
              onPointerDown={stopDrag}
            >
              <ResetIcon />
            </IconButton>
          </Hint>
          <CloseButton
            ariaLabel="Close map"
            onClick={onClose}
            onPointerDown={stopDrag}
          />
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
              <div className="activity-map__group-title t-meta t-label">{title}</div>
              {[...runningRows, ...visibleOlder].map((r) => (
                <ActivityRow key={r.activity_id} record={r} />
              ))}
              {olderRows.length > OLDER_CAP && (
                <Disclosure
                  open={expanded}
                  onToggle={() =>
                    setShowOlder((s) => ({ ...s, [kind]: !expanded }))
                  }
                >
                  {expanded ? "show less" : `+${hiddenCount} older`}
                </Disclosure>
              )}
            </div>
          );
        })}
      </div>
      <ResizeHandles inset onResizeStart={onResizeStart} />
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

  async function onCloseOne() {
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
      <Hint label={detailable ? "Show details" : "Open this work on the cockpit"}>
        <Row
          className={`activity-map__row${failed ? " activity-map__row--failed" : ""}`}
          onClick={() =>
            detailable ? setExpanded((v) => !v) : void openActivity(record)
          }
          ariaExpanded={detailable ? expanded : undefined}
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
            <Hint label="Close this unit">
              <RowActions className="activity-map__row-close-slot">
                <CloseButton
                  size="inline"
                  ariaLabel={`Close ${record.label}`}
                  busy={closing}
                  onClick={onCloseOne}
                />
              </RowActions>
            </Hint>
          )}
          {detailable && (
            <span
              className={`activity-map__chevron t-meta${expanded ? " is-open" : ""}`}
              aria-hidden="true"
            >
              ›
            </span>
          )}
        </Row>
      </Hint>
      {detailable && expanded && <ActivityDetail record={record} />}
    </div>
  );
}
