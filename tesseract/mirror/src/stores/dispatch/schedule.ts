import type {
  Envelope,
  ScheduleJobDoneData,
  ScheduleJobFailedData,
  ScheduleJobStartedData,
  ScheduleStateData,
} from "../../lib/types";
import { startAlarmTone } from "../../lib/alarmSound";
import { useScheduleStore } from "../schedule";
import { useToastStore } from "../toasts";

export function handleSchedule(env: Envelope): void {
  const store = useScheduleStore.getState();
  switch (env.type) {
    case "schedule_state":
      store.applyState(env.data as unknown as ScheduleStateData);
      break;
    case "schedule_job_started":
      store.markStarted(env.data as unknown as ScheduleJobStartedData);
      break;
    case "schedule_job_done":
      store.markDone(env.data as unknown as ScheduleJobDoneData);
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
        .push(`Job failed: ${data.job_name} — ${data.detail}`, "error");
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
        const summary = data.summary ? ` — ${data.summary}` : "";
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
      break;
    }
    default:
      console.debug("[dispatch] unhandled schedule type:", env.type);
  }
}
