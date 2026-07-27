import type {
  CostDeltaData,
  CostOverageAskData,
  CostStateData,
  CostWarningData,
  Envelope,
} from "../../lib/types";
import { useCostStore } from "../cost";
import { useToastStore } from "../toasts";

// Module-scoped flag so the "budget exhausted" sticky toast only fires
// once per blocked-window. Cleared the first time a non-blocked `cost_delta`
// (yaml cap raised, midnight reset, etc.) flows through — otherwise every
// paid turn after the cap would stack duplicate toasts.
let _costBlockedToastShown = false;

export function handleCost(env: Envelope): void {
  if (env.type === "cost_state") {
    const snap = env.data as unknown as CostStateData;
    useCostStore
      .getState()
      .applySnapshot({ timestamp: env.timestamp, data: snap });
    return;
  }
  // Cost UX overhaul — 75% one-shot warning toast.
  if (env.type === "cost_warning") {
    const w = env.data as unknown as CostWarningData;
    const pct = Math.round(w.pct * 100);
    useToastStore
      .getState()
      .push(
        `${w.scope_label} at ${pct}% — $${w.spent_usd.toFixed(2)} / $${w.cap_usd.toFixed(2)}`,
        "warning",
      );
    return;
  }
  // Cost UX overhaul — 100% confirm-to-continue card. CostOverageCard
  // renders against the pending list in cost store.
  if (env.type === "cost_overage_ask") {
    const a = env.data as unknown as CostOverageAskData;
    useCostStore.getState().pushOverageAsk(a);
    return;
  }
  const data = env.data as unknown as CostDeltaData;
  useCostStore.getState().applyDelta({ timestamp: env.timestamp, data });
  if (data.state.blocked && !_costBlockedToastShown) {
    // Name the ceiling that actually tripped — global vs role sub-cap.
    // Matters when e.g. chat_brain=$2/day hits while global=$10/day is
    // barely touched; generic "daily budget exhausted" would mislead.
    const roleCap = data.state.role_cap_usd;
    const roleTripped =
      roleCap !== null && data.state.role_spent_usd >= roleCap;
    const msg = roleTripped
      ? `Role budget exhausted for ${data.role} ($${data.state.role_spent_usd.toFixed(2)} / $${roleCap!.toFixed(2)}) — raise per_role cap in models.yaml, or use delegate_claude / delegate_codex.`
      : `Daily budget exhausted ($${data.state.spent_usd.toFixed(2)} / $${data.state.cap_usd.toFixed(2)}) — use delegate_claude or delegate_codex to route through the CLI subscription, or raise the cap in models.yaml.`;
    useToastStore.getState().pushWith(msg, "error", { sticky: true });
    _costBlockedToastShown = true;
  } else if (!data.state.blocked && _costBlockedToastShown) {
    _costBlockedToastShown = false;
  }
}
