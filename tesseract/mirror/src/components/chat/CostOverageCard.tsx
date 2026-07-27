import { useCostStore } from '../../stores/cost';
import { useWebSocketStore } from '../../stores/websocket';
import type { CostOverageAskData } from '../../lib/types';

interface Props {
  ask: CostOverageAskData;
}

/**
 * CostOverageCard — 100% confirmation prompt rendered inline in the
 * chat view. Operator clicks Yes to keep spending today (extra spend
 * shows in red on HUD chips); No to abort the triggering turn.
 *
 * Backend awaits the response on a future keyed by `call_id`. On Yes
 * we optimistically push the scope into `overageUnlocked` so HUD chips
 * flip to red immediately; backend then sends a fresh `cost_state`
 * envelope which reconciles state authoritatively.
 */
export function CostOverageCard({ ask }: Props) {
  const resolveOverageAsk = useCostStore((s) => s.resolveOverageAsk);
  const unlockOverage = useCostStore((s) => s.unlockOverage);
  const overUsd = ask.spent_usd - ask.cap_usd;

  const send = (approved: boolean) => {
    if (approved) unlockOverage(ask.scope_key);
    useWebSocketStore.getState().sendMessage('cost_overage_response', {
      call_id: ask.call_id,
      approved,
    });
    resolveOverageAsk(ask.call_id);
  };

  return (
    <div className="cost-overage-card" role="alertdialog" aria-live="polite">
      <div className="cost-overage-card__head">
        <span className="cost-overage-card__title">Budget reached</span>
        <span className="cost-overage-card__scope t-meta">{ask.scope_label}</span>
      </div>
      <div className="cost-overage-card__body t-meta">
        Spent <strong>${ask.spent_usd.toFixed(4)}</strong> of cap
        {' '}<strong>${ask.cap_usd.toFixed(2)}</strong>
        {overUsd > 0 && (
          <span className="cost-overage-card__over"> · over by ${overUsd.toFixed(4)}</span>
        )}
        . Continue today? Extra spend will show in red on the HUD until midnight.
      </div>
      <div className="cost-overage-card__actions">
        <button
          type="button"
          className="cost-overage-card__btn cost-overage-card__btn--no"
          onClick={() => send(false)}
        >
          No · stop
        </button>
        <button
          type="button"
          className="cost-overage-card__btn cost-overage-card__btn--yes"
          onClick={() => send(true)}
        >
          Yes · continue
        </button>
      </div>
    </div>
  );
}
