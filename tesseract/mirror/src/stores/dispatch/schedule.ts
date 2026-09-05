import type {
  Envelope,
  ScheduleJobDoneData,
  ScheduleJobFailedData,
  ScheduleJobStartedData,
  ScheduleStateData,
} from "../../lib/types";
import { startAlarmTone } from "../../lib/alarmSound";
import { useAutonomyStore } from "../autonomy";
import { useScheduleStore } from "../schedule";
import { useToastStore } from "../toasts";

/** Autonomy's Managed system room lists these same rows, so anything that
 *  changes one is what tells it to read them again. A timer in the room would
 *  be a guess at how long a run takes, on the panel that exists to stop
 *  guessing. Only once the room has read them: a re-read of nothing is a
 *  request for every session that never opened the panel. */
function rereadManaged(): void {
  const autonomy = useAutonomyStore.getState();
  // An open entry card is looking at ONE of these rows and fetches its own
  // payload, so it is not covered by the room's re-read. It watches this
  // instead: a cadence set from the card comes back as an envelope rather than
  // as a response, and the card reads what the scheduler actually took.
  autonomy.markScheduleTouched();
  if (autonomy.managed.lastFetched === null) return;
  // Rows AND sentence. The store owns what a re-read of this room means,
  // because four callers wanted both and three asked for only the rows.
  void autonomy.rereadManagedRoom();
}

export function handleSchedule(env: Envelope): void {
  const store = useScheduleStore.getState();
  switch (env.type) {
    case "schedule_state": {
      const data = env.data as unknown as ScheduleStateData & { reason?: string };
      // A refused write comes back on this envelope and nothing showed it. The
      // scheduler answers a bad cadence with `schedule_invalid` and its own
      // sentence, and the surface that sent it closed its editor and re-read a
      // row that had not moved, so the operator saw their change quietly
      // undone with no reason given. Only the error path carries `reason`.
      if (data.reason) {
        const named = data.job_name ? `${data.job_name}: ` : "";
        useToastStore.getState().push(`${named}${data.reason}`, "error");
      }
      store.applyState(data);
      rereadManaged();
      break;
    }
    case "schedule_job_started":
      store.markStarted(env.data as unknown as ScheduleJobStartedData);
      // The one the room most needs: a job that just started is the only
      // thing it can report as running, and waiting for the poll would leave
      // a long pass reading as its previous outcome for a minute and a half.
      rereadManaged();
      break;
    case "schedule_job_done":
      store.markDone(env.data as unknown as ScheduleJobDoneData);
      rereadManaged();
      break;
    case "schedule_job_failed": {
      const data = env.data as unknown as ScheduleJobFailedData;
      // Treat alert-mode failures as a done event plus a toast.
      store.markDone({
        job_name: data.job_name,
        run_id: data.run_id,
        ok: false,
        detail: data.detail,
        payload: {},
        duration_ms: 0,
        circuit_broken: data.circuit_broken,
      });
      useToastStore
        .getState()
        .push(`Job failed: ${data.job_name}. ${data.detail}`, "error");
      rereadManaged();
      break;
    }
    case "daily_brief_ready": {
      // MO-9-9 → MO-9-14 — `BriefRenderer.render` finished (cron OR refresh
      // button). The Brief tab is gone; the newsletter card lands as a
      // workspace event via the parallel `workspace_event_appended`
      // envelope. We still surface the toast here so the operator sees
      // an immediate visible signal that the brief landed.
      const data = env.data as { date?: string; summary?: string };
      if (typeof data.date === "string") {
        const summary = data.summary ? `: ${data.summary}` : "";
        useToastStore
          .getState()
          .push(`Daily brief ready: ${data.date}${summary}`, "info");
      }
      break;
    }
    case "schedule_alarm_fired": {
      // v2 envelope: `{ alarm_id, alarm_name, alarm_label, message, recurring,
      // snooze_options }`. alarm_id + snooze_options drive the toast's
      // Snooze/Dismiss buttons; `alarm_name` kept for S4 back-compat.
      const data = env.data as unknown as {
        alarm_id?: string;
        alarm_name?: string;
        alarm_label?: string;
        message?: string;
        recurring?: boolean;
        snooze_options?: string[];
      };
      const label = data.alarm_label ?? data.alarm_name ?? "unnamed";
      const body = data.message?.trim() || `Alarm: ${label}`;
      if (data.alarm_id) {
        useToastStore.getState().pushWith(body, "warning", {
          alarm: {
            id: data.alarm_id,
            label,
            snoozeOptions: data.snooze_options ?? ["5m", "10m", "30m", "1h"],
          },
        });
        startAlarmTone(data.alarm_id);
      } else {
        useToastStore.getState().push(body, "warning");
        // Legacy envelope (no alarm_id): still ring with a synthetic id so
        // the loop's per-id bookkeeping stays consistent. The 30 s safety
        // cap will silence it since no toast button can stop it.
        startAlarmTone("__legacy__");
      }
      // An alarm that went off is one fewer waiting to.
      rereadManaged();
      break;
    }
    default:
      console.debug("[dispatch] unhandled schedule type:", env.type);
  }
}
