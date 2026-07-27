import type {
  Envelope,
  MemorySuggestionData,
  ObserverResultData,
  ObserverUnavailableData,
} from "../../lib/types";
import { useConscienceStore } from "../conscience";
import { useObservationsStore } from "../observations";
import { useSuggestionsStore } from "../suggestions";
import { useToastStore } from "../toasts";

let _observerUnavailableToastShown = false;

export function handleBackground(env: Envelope): void {
  switch (env.type) {
    case "observer_result": {
      const data = env.data as unknown as ObserverResultData;
      const observations = useObservationsStore.getState();
      observations.setPending(false);
      // Successful observer_result proves the observer recovered —
      // re-arm the one-shot toast so a later failure still notifies
      // (fix-pass D2 / Claude coder M-2).
      _observerUnavailableToastShown = false;
      if (data.observation) {
        observations.addObservation({
          mode: data.mode,
          observation: data.observation,
          timestamp: env.timestamp,
        });
      } else {
        useToastStore.getState().push("Observer: nothing notable");
      }
      break;
    }
    case "observer_unavailable": {
      const data = env.data as unknown as ObserverUnavailableData;
      useObservationsStore.getState().setPending(false);
      if (!_observerUnavailableToastShown) {
        const msg =
          data.reason === "observer_error"
            ? "Observer error — check server logs"
            : "Observer unavailable — check API keys";
        useToastStore.getState().push(msg, "error");
        _observerUnavailableToastShown = true;
      }
      break;
    }
    case "memory_suggestion": {
      const data = env.data as unknown as MemorySuggestionData;
      useSuggestionsStore
        .getState()
        .push({ ...data, timestamp: env.timestamp });
      // Owner request 2026-04-29 follow-up — DO NOT toast. Suggestions
      // already render in the right-panel ObserverSuggestions list with
      // a confidence chip; a per-fire toast on top of that just spams the
      // top-right stack on every operator message.
      break;
    }
    case "conscience_drift": {
      // Fired by ConscienceHeartbeatJob on worst-status band transition
      // (ok ↔ warn ↔ bad). Refresh the Conscience tab's store so the
      // cards + pills re-render on next open, and toast the operator —
      // escalation stings, recovery reassures.
      const data = env.data as unknown as {
        from: "ok" | "warn" | "bad";
        to: "ok" | "warn" | "bad";
        changed_signals?: Array<{ name: string; to: string }>;
      };
      useConscienceStore.getState().fetchDrift();
      const toastKind =
        data.to === "bad" ? "error" : data.to === "warn" ? "warning" : "info";
      const names =
        (data.changed_signals ?? []).map((s) => s.name).join(", ") ||
        "signal(s) changed";
      useToastStore
        .getState()
        .push(`Conscience: ${data.from} → ${data.to} (${names})`, toastKind);
      break;
    }
    default:
      console.debug("[dispatch] unhandled background type:", env.type);
  }
}
