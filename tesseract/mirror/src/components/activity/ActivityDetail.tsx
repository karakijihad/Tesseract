// Task 6.2 — extracted from ActivityMap's inline detail block so the chat
// activity taskbar's non-openable rows (routine | autonomy | mcp_session)
// can show the same read-only detail without duplicating the markup.

import type { ActivityRecord } from "../../stores/activity";
import { formatRelative } from "../../lib/time";

interface ActivityDetailProps {
  record: ActivityRecord;
}

export function ActivityDetail({ record }: ActivityDetailProps) {
  const failed = record.state === "failed";
  return (
    <div className="activity-map__detail t-meta">
      <div>state {record.state}</div>
      {record.provider && <div>provider {record.provider}</div>}
      <div>started {formatRelative(record.started_at)}</div>
      <div>updated {formatRelative(record.updated_at)}</div>
      <div>id {record.activity_id}</div>
      {record.parent_session_id && (
        <div>session {record.parent_session_id}</div>
      )}
      {record.parent_turn_id && <div>turn {record.parent_turn_id}</div>}
      {record.transcript_ref && <div>transcript {record.transcript_ref}</div>}
      {failed && record.result && (
        <div className="activity-map__error">{record.result}</div>
      )}
    </div>
  );
}
